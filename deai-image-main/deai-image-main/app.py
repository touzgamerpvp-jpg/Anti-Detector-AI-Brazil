"""
Painel web (Streamlit) para o pipeline de de-fingerprinting de imagens de IA.

Executar a partir da raiz do repositório:
    python -m streamlit run app.py

Fluxo (em lote):
    upload de N imagens -> para cada uma: arquivo temporário -> subprocess (scripts/SCRIPT_NAME)
    -> pós-processamento em memória -> pasta temporária apagada -> entra no ZIP em memória
    -> tabela-resumo + um único download .zip (imagens + relatorio.csv)
"""

# =============================================================================
# >>> CONFIGURAÇÃO PRINCIPAL — ajuste aqui <<<
# =============================================================================
# Nome exato do arquivo dentro da pasta "scripts/".
SCRIPT_NAME = "deai.py"

# Tempo máximo (segundos) que o script pode rodar antes de ser encerrado.
TIMEOUT_SEGUNDOS = 180

# Perfil Analógico Avançado: intensidade do deai.py usada como base. "light" evita
# empilhar o ruído pesado do script com o ruído de luminância do painel.
PERFIL_ANALOGICO_BASE = "light"
# Desvio padrão (0-255) do ruído gaussiano injetado só no canal Y no perfil analógico.
SIGMA_RUIDO_Y_MAX = 3.5
# Faixa (inclusiva) da qualidade do JPEG final no perfil analógico; sorteada a cada exportação.
QUALIDADE_JPEG_ANALOGICO = (78, 82)
# Subamostragem de croma do JPEG final: 0 = 4:4:4, 1 = 4:2:2, 2 = 4:2:0 (padrão do Pillow).
SUBSAMPLING_JPEG_ANALOGICO = 2
# Qualidade do JPEG final nos demais modos com emulação de sensor.
QUALIDADE_JPEG_PADRAO = 92
# =============================================================================

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

# Pillow e NumPy são os mesmos requisitos do deai.py; aqui servem para a análise
# forense (metadados e diferença de pixels). Se faltarem, o painel avisa em vez de quebrar.
try:
    import numpy as np
    from PIL import ExifTags, Image, ImageFilter, UnidentifiedImageError

    DEPENDENCIAS_OK = True
except ImportError as erro_import:  # pragma: no cover - depende do ambiente
    DEPENDENCIAS_OK = False
    ERRO_IMPORT = str(erro_import)

try:
    from scripts.supabase_client import SupabaseManager
except ImportError:
    try:
        from supabase_client import SupabaseManager
    except ImportError:
        SupabaseManager = None


def obter_supabase(url_override: str = "", key_override: str = "", bucket_override: str = "deai-images"):
    if SupabaseManager is None:
        return None
    url = url_override
    key = key_override
    bucket = bucket_override or "deai-images"

    if not url:
        if hasattr(st, "secrets"):
            if "supabase" in st.secrets:
                url = st.secrets["supabase"].get("url", "")
                key = key or st.secrets["supabase"].get("key", "")
                bucket = st.secrets["supabase"].get("bucket", bucket)
            elif "SUPABASE_URL" in st.secrets:
                url = st.secrets.get("SUPABASE_URL", "")
                key = key or st.secrets.get("SUPABASE_KEY", "")

    if not url:
        url = os.environ.get("SUPABASE_URL", "")
        key = key or os.environ.get("SUPABASE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")

    return SupabaseManager(url=url, key=key, bucket=bucket)


# -----------------------------------------------------------------------------
# Constantes derivadas
# -----------------------------------------------------------------------------
RAIZ_PROJETO = Path(__file__).resolve().parent
CAMINHO_SCRIPT = RAIZ_PROJETO / "scripts" / SCRIPT_NAME

# Rótulo exibido no slider -> modo interno. Os quatro primeiros vão direto para o
# --strength do script; "analog" é um perfil do painel (ver PERFIL_ANALOGICO_BASE).
INTENSIDADES = {
    "Suave (Preserva qualidade)": "light",
    "Média (Padrão)": "medium",
    "Agressiva (Forte)": "heavy",
    "Ultra Evasão (Extrema)": "extreme",
    "Perfil Analógico Avançado": "analog",
}

# O que cada intensidade faz de fato (espelha STRENGTH_CONFIGS do deai.py).
DETALHES_INTENSIDADE = {
    "light": "Ruído σ 1,5 · contraste ×1,03 · resize 99% · JPEG 88→96",
    "medium": "Ruído σ 2,5 · contraste ×1,05 · resize 97% · JPEG 80→94",
    "heavy": "Ruído σ 7,5 · contraste ×1,08 · resize 90% · JPEG 60→65",
    "extreme": "Ruído σ 9,5 · 2 ciclos blur/sharpen · resize 88% · JPEG 55→55",
    "analog": (
        f"Script em '{PERFIL_ANALOGICO_BASE}' + ruído só no canal Y (σ {SIGMA_RUIDO_Y_MAX}) "
        f"+ JPEG progressivo/otimizado q{QUALIDADE_JPEG_ANALOGICO[0]}–{QUALIDADE_JPEG_ANALOGICO[1]}"
    ),
}

TIPOS_ACEITOS = ["png", "jpg", "jpeg", "webp"]


# -----------------------------------------------------------------------------
# Verificação de ambiente
# -----------------------------------------------------------------------------
def localizar_exiftool() -> str | None:
    """Devolve o caminho do ExifTool, ou None se não estiver instalado.

    No Windows, um terminal aberto antes da instalação (winget) não enxerga o PATH novo.
    Nesse caso relemos o PATH direto do registro (sistema + usuário).
    """
    encontrado = shutil.which("exiftool")
    if encontrado or os.name != "nt":
        return encontrado

    import winreg

    caminhos = []
    chaves = [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ]
    for raiz, subchave in chaves:
        try:
            with winreg.OpenKey(raiz, subchave) as chave:
                caminhos.append(os.path.expandvars(winreg.QueryValueEx(chave, "Path")[0]))
        except OSError:
            pass
    return shutil.which("exiftool", path=os.pathsep.join(caminhos))


@st.cache_data(ttl=60)
def verificar_ambiente() -> dict:
    """Checa se o script existe, se o Python do subprocesso tem as libs e se o ExifTool está no PATH.

    O cache de 60 s evita repetir a checagem a cada interação da página.
    """
    status = {
        "script": CAMINHO_SCRIPT.is_file(),
        "exiftool": localizar_exiftool(),  # caminho completo ou None
        "libs_subprocesso": False,
        "erro_libs": "",
    }

    # O subprocesso usa o mesmo interpretador do Streamlit (sys.executable),
    # então testamos as importações exatamente nele.
    try:
        teste = subprocess.run(
            [sys.executable, "-c", "import PIL, numpy"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status["libs_subprocesso"] = teste.returncode == 0
        status["erro_libs"] = teste.stderr.strip().splitlines()[-1] if teste.stderr.strip() else ""
    except (OSError, subprocess.TimeoutExpired) as erro:
        status["erro_libs"] = str(erro)

    return status


# -----------------------------------------------------------------------------
# Execução do script em segundo plano
# -----------------------------------------------------------------------------
def _ler_fluxo(fluxo, destino: list) -> None:
    """Lê um pipe linha a linha (roda em thread própria para não travar a interface)."""
    for linha in iter(fluxo.readline, ""):
        destino.append(linha.rstrip("\n"))
    fluxo.close()


def executar_script(entrada: Path, saida: Path, strength: str, somente_metadados: bool, painel_log) -> dict:
    """Dispara o script via subprocess.Popen e transmite o log ao vivo para `painel_log`.

    Por que Popen + threads e não asyncio: o Streamlit reexecuta o app.py de cima a baixo
    a cada interação, de forma síncrona. Popen já roda o processo em paralelo; as threads
    drenam stdout/stderr (evitando deadlock de buffer cheio) e o loop principal só atualiza
    a tela e controla o timeout.
    """
    comando = [sys.executable, "-u", str(CAMINHO_SCRIPT), str(entrada), "-o", str(saida)]
    if somente_metadados:
        comando.append("--no-metadata")
    else:
        comando += ["--strength", strength, "-v"]

    # O deai.py imprime "✓" e "⚠️". No Windows, com a saída redirecionada para pipe,
    # o Python usa cp1252 e quebra com UnicodeEncodeError. Forçar UTF-8 resolve.
    ambiente = os.environ.copy()
    ambiente["PYTHONUTF8"] = "1"
    ambiente["PYTHONIOENCODING"] = "utf-8"

    # O script chama "exiftool" pelo nome; garantimos que a pasta dele esteja no PATH do subprocesso.
    exiftool = verificar_ambiente()["exiftool"]
    if exiftool:
        ambiente["PATH"] = str(Path(exiftool).parent) + os.pathsep + ambiente.get("PATH", "")

    linhas_stdout: list[str] = []
    linhas_stderr: list[str] = []

    processo = subprocess.Popen(
        comando,
        cwd=RAIZ_PROJETO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ambiente,
    )

    leitores = [
        threading.Thread(target=_ler_fluxo, args=(processo.stdout, linhas_stdout), daemon=True),
        threading.Thread(target=_ler_fluxo, args=(processo.stderr, linhas_stderr), daemon=True),
    ]
    for leitor in leitores:
        leitor.start()

    inicio = time.monotonic()
    estourou_tempo = False
    exibidas = -1

    # Loop de acompanhamento: atualiza o log só quando chegam linhas novas.
    while processo.poll() is None:
        if time.monotonic() - inicio > TIMEOUT_SEGUNDOS:
            processo.kill()
            estourou_tempo = True
            break
        if len(linhas_stdout) != exibidas:
            exibidas = len(linhas_stdout)
            painel_log.code("\n".join(linhas_stdout[-25:]) or "Iniciando…", language="text")
        time.sleep(0.15)

    processo.wait()
    for leitor in leitores:
        leitor.join(timeout=5)

    # O deai.py cria %TEMP%/deai_<pid> e só apaga quando termina com sucesso.
    # Como o PID dele é o PID do subprocesso, removemos qualquer sobra aqui.
    shutil.rmtree(Path(tempfile.gettempdir()) / f"deai_{processo.pid}", ignore_errors=True)

    painel_log.code("\n".join(linhas_stdout[-25:]) or "(sem saída)", language="text")

    return {
        "codigo": processo.returncode,
        "stdout": "\n".join(linhas_stdout),
        "stderr": "\n".join(linhas_stderr),
        "timeout": estourou_tempo,
        "duracao": time.monotonic() - inicio,
        "comando": comando,
    }


# -----------------------------------------------------------------------------
# Pós-processamento: emulação de sensor e lente físicos
# -----------------------------------------------------------------------------
def _deslocar_canal(canal: np.ndarray, dx: int) -> np.ndarray:
    """Translada um canal (H×W) dx pixels na horizontal (positivo = direita).

    Repete o pixel da borda em vez de "dar a volta" como np.roll, que traria a
    coluna do lado oposto da imagem para dentro do quadro.
    """
    if dx == 0:
        return canal
    largura = canal.shape[1]
    expandido = np.pad(canal, ((0, 0), (abs(dx), abs(dx))), mode="edge")
    inicio = abs(dx) - dx
    return expandido[:, inicio : inicio + largura]


def emular_sensor_optico(
    img: "Image.Image",
    deslocamento_ca: int = 1,
    escala_reamostragem: float = 0.80,
    sigma_grao: float = 3.0,
) -> "Image.Image":
    """Aplica imperfeições ópticas de câmera real sobre uma imagem RGB (em memória).

    Ordem das etapas (cada uma prepara a seguinte):
        1. Aberração cromática — R e B deslocados em sentidos opostos em relação ao G.
        2. Reamostragem — redução BILINEAR + ampliação BICUBIC ao tamanho original.
        3. Nitidez só nas bordas — devolve o contorno amaciado pela reamostragem.
        4. Grão monocromático — por último, para o grão não ser afiado pela etapa 3.
    """
    largura, altura = img.size

    # 1) Aberração cromática lateral: vermelho para a direita, azul para a esquerda,
    #    verde como referência — a franja colorida surge só onde há contraste.
    arr = np.asarray(img)
    img = Image.fromarray(
        np.stack(
            [
                _deslocar_canal(arr[..., 0], deslocamento_ca),
                arr[..., 1],
                _deslocar_canal(arr[..., 2], -deslocamento_ca),
            ],
            axis=-1,
        )
    )

    # 2) Reamostragem: o BILINEAR na redução descarta as frequências mais altas;
    #    o BICUBIC na ampliação reconstrói com uma curva de interpolação diferente.
    tamanho_reduzido = (max(1, round(largura * escala_reamostragem)), max(1, round(altura * escala_reamostragem)))
    img = img.resize(tamanho_reduzido, Image.Resampling.BILINEAR).resize((largura, altura), Image.Resampling.BICUBIC)

    # 3) Nitidez restrita às transições: máscara de bordas (FIND_EDGES) suavizada e
    #    amplificada decide onde entra a versão com UnsharpMask; áreas lisas ficam intactas.
    nitida = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=70, threshold=2))
    mascara = (
        img.convert("L")
        .filter(ImageFilter.FIND_EDGES)
        .filter(ImageFilter.GaussianBlur(1.5))
        .point(lambda valor: min(255, valor * 4))
    )
    img = Image.composite(nitida, img, mascara)

    # 4) Grão monocromático: o MESMO ruído somado aos três canais (sem ruído colorido),
    #    mais forte nos meios-tons e mais fraco em pretos/brancos, como no filme.
    arr = np.asarray(img, dtype=np.float32)
    luminancia = arr.mean(axis=-1, keepdims=True) / 255.0
    peso_meios_tons = 0.4 + 0.6 * (1.0 - (2.0 * luminancia - 1.0) ** 2)
    grao = np.random.default_rng().normal(0.0, sigma_grao, (altura, largura, 1)).astype(np.float32)
    return Image.fromarray(np.clip(arr + grao * peso_meios_tons, 0, 255).astype(np.uint8))


def injetar_ruido_luminancia(img: "Image.Image", sigma: float = SIGMA_RUIDO_Y_MAX) -> "Image.Image":
    """Soma ruído gaussiano SOMENTE no canal Y (luminância) do espaço YCbCr.

    Cb e Cr são recombinados byte a byte, sem alteração. A volta para RGB introduz
    apenas o arredondamento da conversão (±1 nível), não deslocamento de cor.
    Combina com o JPEG final: o encoder subamostra a crominância (4:2:0), mas guarda
    o Y em resolução total — o ruído sobrevive exatamente no canal que o recebeu.
    """
    y, cb, cr = img.convert("YCbCr").split()
    luminancia = np.asarray(y, dtype=np.float32)
    ruido = np.random.default_rng().normal(0.0, sigma, luminancia.shape).astype(np.float32)
    y_ruidoso = Image.fromarray(np.clip(luminancia + ruido, 0, 255).astype(np.uint8))
    return Image.merge("YCbCr", (y_ruidoso, cb, cr)).convert("RGB")


def suavizar_bordas(img: "Image.Image", intensidade: float = 0.5) -> "Image.Image":
    """Atenua o micro-contraste nas transições (halos de nitidez), sem tocar nas áreas lisas.

    Kernel gaussiano 3×3 [1 2 1 / 2 4 2 / 1 2 1] ÷ 16 aplicado através de uma máscara de
    bordas: onde a máscara é 0 (áreas lisas) a imagem fica como está.
    """
    suave = img.filter(ImageFilter.Kernel((3, 3), [1, 2, 1, 2, 4, 2, 1, 2, 1], scale=16))
    mascara = (
        img.convert("L")
        .filter(ImageFilter.FIND_EDGES)
        .filter(ImageFilter.GaussianBlur(1.0))
        .point(lambda valor: min(255, int(valor * 4 * intensidade)))
    )
    return Image.composite(suave, img, mascara)


def recodificar_jpeg_multipass(
    img: "Image.Image", qualidade: int | None = None, subsampling: int = SUBSAMPLING_JPEG_ANALOGICO
) -> tuple[bytes, int]:
    """Codifica a imagem final como JPEG progressivo com tabelas de Huffman otimizadas.

    1. Grava em PNG num buffer em memória (sem perdas) e relê como array NumPy:
       a imagem sai "limpa" — RGB uint8 puro, sem ICC, EXIF ou info herdados.
    2. Codifica o array em JPEG com optimize=True (Huffman calculado para esta imagem,
       não a tabela genérica) e progressive=True (varreduras em camadas de detalhe).

    Observação: nenhuma dessas opções altera a tabela de QUANTIZAÇÃO — ela é definida
    pelo parâmetro `qualidade` (tabelas padrão do libjpeg escaladas).

    Sem `qualidade`, sorteia um valor dentro de QUALIDADE_JPEG_ANALOGICO.
    Devolve (bytes do JPEG, qualidade usada).
    """
    if qualidade is None:
        minima, maxima = QUALIDADE_JPEG_ANALOGICO
        qualidade = int(np.random.default_rng().integers(minima, maxima + 1))

    buffer_png = io.BytesIO()
    img.save(buffer_png, "PNG")
    buffer_png.seek(0)
    with Image.open(buffer_png) as relida:
        pixels = np.asarray(relida.convert("RGB"), dtype=np.uint8)

    saida = io.BytesIO()
    Image.fromarray(pixels).save(
        saida, "JPEG", quality=qualidade, optimize=True, progressive=True, subsampling=subsampling
    )
    return saida.getvalue(), qualidade


def aplicar_pos_processamento(conteudo: bytes, sensor: dict | None, perfil_analogico: bool) -> tuple[bytes, list[str]]:
    """Encadeia as rotinas do painel sobre a saída do script, com UM único JPEG no final.

    Devolve (bytes do JPEG, descrição das etapas aplicadas, para o log).
    """
    with Image.open(io.BytesIO(conteudo)) as original:
        img = original.convert("RGB")
    etapas = []

    if sensor:
        img = emular_sensor_optico(img, **sensor)
        etapas.append(
            f"sensor óptico (aberração {sensor['deslocamento_ca']} px · "
            f"reamostragem {sensor['escala_reamostragem']:.0%} · grão σ {sensor['sigma_grao']})"
        )

    if perfil_analogico:
        # Suavização ANTES do ruído: depois dele, o filtro também apagaria o ruído do Y.
        img = suavizar_bordas(img)
        etapas.append("suavização de bordas (kernel 3×3)")
        img = injetar_ruido_luminancia(img, SIGMA_RUIDO_Y_MAX)
        etapas.append(f"ruído só no canal Y (σ {SIGMA_RUIDO_Y_MAX})")
        jpeg, qualidade = recodificar_jpeg_multipass(img)
        etapas.append(f"JPEG progressivo/otimizado via buffer PNG (q{qualidade}, subsampling {SUBSAMPLING_JPEG_ANALOGICO})")
        return jpeg, etapas

    # Sem o perfil analógico: JPEG simples. Não repassa exif/xmp/icc, então sai sem metadados.
    saida = io.BytesIO()
    img.save(saida, "JPEG", quality=QUALIDADE_JPEG_PADRAO, optimize=True)
    etapas.append(f"JPEG q{QUALIDADE_JPEG_PADRAO}")
    return saida.getvalue(), etapas


def processar_upload(arquivo, strength: str, somente_metadados: bool, painel_log, pos_processamento: dict | None = None) -> dict:
    """Salva o upload em pasta temporária, roda o script e devolve o resultado EM MEMÓRIA.

    A pasta temporária é apagada ao sair do bloco `with`, com sucesso ou com erro.
    O download é servido a partir dos bytes em memória, então nada fica no disco
    esperando o usuário clicar em "Baixar".
    """
    sufixo = Path(arquivo.name).suffix.lower() or ".png"

    # "analog" não existe no script: ele roda na intensidade-base e o painel faz o resto.
    perfil_analogico = strength == "analog" and not somente_metadados
    strength_script = PERFIL_ANALOGICO_BASE if strength == "analog" else strength

    with tempfile.TemporaryDirectory(prefix="deai_painel_") as pasta_tmp:
        pasta = Path(pasta_tmp)
        # Nome fixo: evita espaços/acentos do nome original quebrando o comando.
        entrada = pasta / f"entrada{sufixo}"
        # O pipeline completo sempre grava JPEG; o modo só-metadados mantém o formato original.
        saida = pasta / (f"saida{sufixo}" if somente_metadados else "saida.jpg")

        entrada.write_bytes(arquivo.getvalue())
        execucao = executar_script(entrada, saida, strength_script, somente_metadados, painel_log)

        # Só consideramos sucesso se o código de saída for 0 E o arquivo existir.
        execucao["sucesso"] = execucao["codigo"] == 0 and not execucao["timeout"] and saida.is_file()
        execucao["bytes_saida"] = saida.read_bytes() if execucao["sucesso"] else None
        execucao["extensao_saida"] = saida.suffix

    # Pós-processamento em memória, sobre a saída do script (nunca no modo só-metadados).
    sensor = pos_processamento if not somente_metadados else None
    execucao["etapas_pos"] = []
    if execucao["sucesso"] and (sensor or perfil_analogico):
        try:
            execucao["bytes_saida"], execucao["etapas_pos"] = aplicar_pos_processamento(
                execucao["bytes_saida"], sensor, perfil_analogico
            )
            execucao["extensao_saida"] = ".jpg"
            execucao["stdout"] += "\n\n[painel] Pós-processamento: " + " → ".join(execucao["etapas_pos"])
        except (OSError, ValueError, UnidentifiedImageError, MemoryError) as erro:
            # Se alguma rotina falhar (arquivo ilegível, imagem grande demais para a RAM...),
            # entrega a saída do script e registra o motivo no stderr exibido pelo painel.
            execucao["etapas_pos"] = []
            execucao["stderr"] += f"\n[painel] Pós-processamento falhou, mantida a saída do script: {erro}"

    execucao["modo"] = (
        "metadados"
        if somente_metadados
        else strength
        + (f" ({strength_script})" if strength == "analog" else "")
        + (" + sensor" if sensor and execucao["etapas_pos"] else "")
    )
    return execucao


# -----------------------------------------------------------------------------
# Análise forense (antes x depois)
# -----------------------------------------------------------------------------
def analisar_metadados(conteudo: bytes) -> dict:
    """Levanta os vestígios de proveniência que o arquivo carrega."""
    relatorio = {"formato": "?", "dimensoes": "?", "exif": {}, "blocos_texto": [], "c2pa": False, "xmp": False}

    # Assinaturas de C2PA/Content Credentials (caixas JUMBF) e XMP no binário bruto.
    relatorio["c2pa"] = b"c2pa" in conteudo or b"jumb" in conteudo
    relatorio["xmp"] = b"http://ns.adobe.com/xap/1.0/" in conteudo or b"<x:xmpmeta" in conteudo

    try:
        with Image.open(io.BytesIO(conteudo)) as img:
            relatorio["formato"] = img.format or "?"
            relatorio["dimensoes"] = f"{img.width}×{img.height} px · {img.mode}"

            for tag_id, valor in img.getexif().items():
                nome = ExifTags.TAGS.get(tag_id, f"Tag {tag_id}")
                texto = valor.decode("utf-8", "replace") if isinstance(valor, bytes) else str(valor)
                relatorio["exif"][nome] = texto[:120]

            # Chunks de texto do PNG: Stable Diffusion/ComfyUI gravam o prompt inteiro aqui
            # ("parameters", "prompt", "workflow").
            ignorar = {"exif", "xmp", "icc_profile", "dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density", "progressive", "progression", "adobe", "adobe_transform"}
            relatorio["blocos_texto"] = [chave for chave in img.info if chave not in ignorar]
    except (UnidentifiedImageError, OSError):
        pass

    return relatorio


def medir_diferenca(original: bytes, processada: bytes) -> dict | None:
    """Mede quanto a imagem mudou: PSNR (dB) e diferença média por canal (0-255)."""
    try:
        with Image.open(io.BytesIO(original)) as a, Image.open(io.BytesIO(processada)) as b:
            a_rgb, b_rgb = a.convert("RGB"), b.convert("RGB")
            if a_rgb.size != b_rgb.size:
                b_rgb = b_rgb.resize(a_rgb.size)
            arr_a = np.asarray(a_rgb, dtype=np.float32)
            arr_b = np.asarray(b_rgb, dtype=np.float32)
    except (UnidentifiedImageError, OSError):
        return None

    mse = float(np.mean((arr_a - arr_b) ** 2))
    psnr = float("inf") if mse == 0 else 10 * np.log10(255.0**2 / mse)
    return {"psnr": psnr, "diferenca_media": float(np.mean(np.abs(arr_a - arr_b)))}


def resumir_metadados(relatorio: dict) -> str:
    """Resume em uma linha os vestígios de proveniência presentes ("—" se nenhum)."""
    marcadores = [
        ("EXIF", bool(relatorio["exif"])),
        ("C2PA", relatorio["c2pa"]),
        ("XMP", relatorio["xmp"]),
        ("Texto", bool(relatorio["blocos_texto"])),
    ]
    return ", ".join(nome for nome, presente in marcadores if presente) or "—"


# -----------------------------------------------------------------------------
# Processamento em lote + empacotamento ZIP
# -----------------------------------------------------------------------------
def nome_unico(nome: str, usados: set[str]) -> str:
    """Evita que duas saídas com o mesmo nome se sobrescrevam dentro do ZIP.

    Ex.: "foto.png" e "foto.jpg" viram "processada_foto.jpg" e "processada_foto_2.jpg".
    A comparação ignora maiúsculas, como o Explorador do Windows.
    """
    base, extensao = Path(nome).stem, Path(nome).suffix
    candidato, contador = nome, 2
    while candidato.lower() in usados:
        candidato = f"{base}_{contador}{extensao}"
        contador += 1
    usados.add(candidato.lower())
    return candidato


def gerar_csv(linhas: list[dict]) -> bytes:
    """Relatório do lote em CSV (separador ";" e BOM UTF-8: o Excel pt-BR abre com acentos certos)."""
    buffer = io.StringIO()
    escritor = csv.DictWriter(buffer, fieldnames=list(linhas[0].keys()), delimiter=";")
    escritor.writeheader()
    escritor.writerows(linhas)
    return buffer.getvalue().encode("utf-8-sig")


def nome_no_zip(nome_original: str, extensao_saida: str) -> str:
    """Nome dentro do ZIP: prefixo "processada_" + nome original.

    A extensão acompanha o formato REAL da saída: o pipeline completo sempre gera JPEG,
    então "foto.png" vira "processada_foto.jpg" (um .png com conteúdo JPEG enganaria
    outros programas). No modo só-metadados o formato é mantido: "processada_foto.png".
    """
    return f"processada_{Path(nome_original).stem}{extensao_saida}"


def processar_lote(
    arquivos: list,
    strength: str,
    somente_metadados: bool,
    pos_processamento: dict | None,
    barra,
    mensagem,
    painel_log,
    supabase_mgr=None,
) -> dict:
    """Percorre os arquivos enviados, processa um a um e grava as saídas num ZIP em memória.

    Uma falha em um arquivo não interrompe o lote: ela é registrada no relatório
    e o loop segue para o próximo. Só as saídas bem-sucedidas entram no ZIP.
    Se o Supabase estiver configurado, envia as imagens para o Storage e registra no DB.
    """
    buffer_zip = io.BytesIO()
    linhas: list[dict] = []
    falhas: list[dict] = []
    itens_processados: list[dict] = []
    nomes_usados: set[str] = set()
    total = len(arquivos)
    inicio = time.monotonic()

    with zipfile.ZipFile(buffer_zip, "w") as pacote:
        for indice, arquivo in enumerate(arquivos, start=1):
            barra.progress((indice - 1) / total)
            mensagem.markdown(f"Processando imagem **{indice} de {total}**: `{arquivo.name}`")
            bytes_originais = arquivo.getvalue()

            linha = {
                "arquivo": arquivo.name,
                "status": "",
                "saida": "",
                "modo": "",
                "kb_antes": round(len(bytes_originais) / 1024, 1),
                "kb_depois": None,
                "psnr_db": None,
                "metadados_antes": resumir_metadados(analisar_metadados(bytes_originais)),
                "metadados_depois": "",
                "segundos": None,
                "url_publica": None,
            }

            execucao, motivo = None, ""
            try:
                execucao = processar_upload(arquivo, strength, somente_metadados, painel_log, pos_processamento)
            except OSError as erro:
                # Inclui FileNotFoundError (Python/script sumiu), disco cheio, permissão negada.
                motivo = f"erro de sistema: {erro}"

            if execucao and execucao["sucesso"]:
                nome_saida = nome_unico(nome_no_zip(arquivo.name, execucao["extensao_saida"]), nomes_usados)
                # JPEG/PNG já são comprimidos: ZIP_STORED só empacota, sem gastar CPU à toa.
                pacote.writestr(nome_saida, execucao["bytes_saida"], compress_type=zipfile.ZIP_STORED)

                diferenca = medir_diferenca(bytes_originais, execucao["bytes_saida"])
                psnr = diferenca["psnr"] if diferenca else None
                mime_saida = "image/jpeg" if execucao["extensao_saida"].lower() in [".jpg", ".jpeg"] else "image/png"

                # Envio opcional para o Supabase
                storage_path, url_publica = None, None
                if supabase_mgr and supabase_mgr.is_configured:
                    ok_up, storage_path, url_publica = supabase_mgr.upload_imagem(
                        nome_saida, execucao["bytes_saida"], mime_saida
                    )
                    if ok_up:
                        linha["url_publica"] = url_publica
                        linha["storage_path"] = storage_path

                linha.update(
                    status="ok",
                    saida=nome_saida,
                    modo=execucao["modo"],
                    kb_depois=round(len(execucao["bytes_saida"]) / 1024, 1),
                    psnr_db=round(psnr, 1) if psnr is not None and psnr != float("inf") else psnr,
                    metadados_depois=resumir_metadados(analisar_metadados(execucao["bytes_saida"])),
                    segundos=round(execucao["duracao"], 1),
                )

                if supabase_mgr and supabase_mgr.is_configured:
                    supabase_mgr.salvar_historico(linha)

                itens_processados.append({
                    "nome": nome_saida,
                    "arquivo_original": arquivo.name,
                    "bytes": execucao["bytes_saida"],
                    "extensao": execucao["extensao_saida"],
                    "mime": mime_saida,
                    "kb": round(len(execucao["bytes_saida"]) / 1024, 1),
                    "psnr": round(psnr, 1) if psnr is not None and psnr != float("inf") else psnr,
                    "url_publica": url_publica,
                })
            else:
                if execucao:
                    motivo = f"timeout ({TIMEOUT_SEGUNDOS} s)" if execucao["timeout"] else f"script terminou com código {execucao['codigo']}"
                    # O deai.py escreve alguns erros no stdout (ex.: "Cannot open image"): guardamos os dois.
                    log = "\n".join(parte for parte in (execucao["stderr"], execucao["stdout"]) if parte)
                else:
                    log = motivo
                linha["status"] = f"falha: {motivo}"
                falhas.append({"arquivo": arquivo.name, "motivo": motivo, "log": log[-3000:]})

            linhas.append(linha)

        barra.progress(1.0)
        mensagem.markdown(f"Lote concluído: **{total - len(falhas)} de {total}** processadas.")
        pacote.writestr("relatorio.csv", gerar_csv(linhas), compress_type=zipfile.ZIP_DEFLATED)

    return {
        "zip": buffer_zip.getvalue(),
        "linhas": linhas,
        "falhas": falhas,
        "itens_processados": itens_processados,
        "ok": total - len(falhas),
        "total": total,
        "duracao": time.monotonic() - inicio,
    }


# -----------------------------------------------------------------------------
# Interface
# -----------------------------------------------------------------------------
# Visual: fundo "aurora" (três radiais violeta/ciano/magenta sobre quase-preto),
# superfícies em vidro fosco e o mesmo degradê nos títulos, botões e barra de progresso.
ESTILO = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
    --bg: #07070d;
    --vidro: rgba(20, 20, 34, 0.55);
    --vidro-forte: rgba(16, 16, 28, 0.78);
    --borda: rgba(255, 255, 255, 0.08);
    --borda-forte: rgba(167, 139, 250, 0.35);
    --texto: #e7e7f0;
    --texto-2: #9a9ab0;
    --violeta: #8b5cf6;
    --ciano: #22d3ee;
    --magenta: #ec4899;
    --ok: #34d399;
    --alerta: #fbbf24;
    --erro: #f87171;
    --degrade: linear-gradient(120deg, #8b5cf6 0%, #ec4899 50%, #22d3ee 100%);
}

html, body, .stApp, .stMarkdown p, label, input, textarea {
    font-family: 'Inter', system-ui, sans-serif;
}
h1, h2, h3, h4 { font-family: 'Space Grotesk', 'Inter', sans-serif !important; letter-spacing: -0.02em; }
code, pre, .stCode { font-family: 'JetBrains Mono', ui-monospace, monospace !important; }

/* ---------- fundo aurora ---------- */
[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(1000px 700px at 30% -15%, rgba(139, 92, 246, 0.38), transparent 60%),
        radial-gradient(800px 600px at 105% 15%, rgba(34, 211, 238, 0.22), transparent 60%),
        radial-gradient(900px 700px at 55% 115%, rgba(236, 72, 153, 0.24), transparent 60%),
        var(--bg);
    background-attachment: fixed;
}
[data-testid="stAppViewContainer"]::before {
    /* grade sutil que dá textura ao fundo sem competir com o conteúdo */
    content: ""; position: fixed; inset: 0; pointer-events: none;
    background-image:
        linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
    background-size: 48px 48px;
    mask-image: radial-gradient(ellipse at 50% 0%, black 30%, transparent 75%);
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"], .block-container { padding-top: 2.5rem; max-width: 1180px; }

/* ---------- barra lateral em vidro ---------- */
[data-testid="stSidebar"] {
    background: var(--vidro-forte);
    backdrop-filter: blur(18px) saturate(140%);
    border-right: 1px solid var(--borda);
}
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
    font-size: 0.78rem !important; text-transform: uppercase; letter-spacing: 0.14em !important;
    color: var(--texto-2) !important; font-weight: 600 !important; margin-bottom: 0.25rem;
}
[data-testid="stSidebar"] hr { border-color: var(--borda); margin: 1.1rem 0; }

/* ---------- hero ---------- */
.hero { position: relative; padding: 2.2rem 2.2rem 2rem; border-radius: 22px; margin-bottom: 1.6rem;
    background: var(--vidro); border: 1px solid var(--borda); backdrop-filter: blur(14px); overflow: hidden; }
.hero::after { content: ""; position: absolute; inset: 0; border-radius: inherit; padding: 1px; pointer-events: none;
    background: linear-gradient(120deg, rgba(139,92,246,.6), rgba(236,72,153,.25), rgba(34,211,238,.5));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; }
.hero-tag { display: inline-flex; align-items: center; gap: .5rem; font-size: .72rem; font-weight: 600;
    letter-spacing: .14em; text-transform: uppercase; color: #c4b5fd; padding: .35rem .75rem; border-radius: 999px;
    background: rgba(139, 92, 246, 0.12); border: 1px solid rgba(139, 92, 246, 0.3); }
.hero-tag i { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 10px var(--ok); }
.hero h1 { font-size: clamp(2rem, 4.2vw, 3.1rem) !important; font-weight: 700 !important; line-height: 1.05 !important;
    margin: 1rem 0 .7rem !important; padding: 0 !important;
    background: linear-gradient(120deg, #fff 0%, #ddd6fe 35%, #f0abfc 65%, #67e8f9 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent !important; }
.hero p { color: var(--texto-2); font-size: 1.02rem; max-width: 680px; margin: 0; line-height: 1.55; }
.hero-chips { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1.3rem; }
.chip { font-size: .78rem; color: var(--texto); padding: .38rem .8rem; border-radius: 999px;
    background: rgba(255,255,255,0.04); border: 1px solid var(--borda); }
.chip b { color: #c4b5fd; font-weight: 600; }

/* ---------- passos (estado vazio) ---------- */
.passos { display: grid; grid-template-columns: repeat(3, 1fr); gap: .9rem; margin-top: 1rem; }
.passo { padding: 1.2rem 1.25rem; border-radius: 16px; background: var(--vidro); border: 1px solid var(--borda); }
.passo .num { display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 8px;
    font: 600 .85rem 'Space Grotesk', sans-serif; color: #fff; background: var(--degrade); }
.passo .tit { margin: .8rem 0 .3rem; font: 600 1rem 'Space Grotesk', sans-serif; color: var(--texto); }
.passo p { margin: 0; color: var(--texto-2); font-size: .88rem; line-height: 1.5; }
@media (max-width: 760px) { .passos { grid-template-columns: 1fr; } .hero { padding: 1.5rem; } }

/* ---------- uploader ---------- */
[data-testid="stFileUploader"] label p { font-weight: 600; color: var(--texto); }
[data-testid="stFileUploaderDropzone"] {
    background: linear-gradient(var(--vidro-forte), var(--vidro-forte)) padding-box,
                linear-gradient(120deg, rgba(139,92,246,.55), rgba(236,72,153,.35), rgba(34,211,238,.5)) border-box;
    border: 1.5px dashed transparent; border-radius: 18px; padding: 2.4rem 1.6rem; transition: all .2s ease;
}
[data-testid="stFileUploaderDropzone"]:hover { box-shadow: 0 0 0 4px rgba(139,92,246,.12), 0 10px 40px -10px rgba(139,92,246,.45); }

/* ---------- botões ---------- */
[data-testid="stBaseButton-primary"] {
    background: var(--degrade) !important; background-size: 200% 100% !important; border: 0 !important;
    color: #fff !important; font-weight: 600 !important; letter-spacing: .01em; min-height: 3rem; border-radius: 14px !important;
    box-shadow: 0 10px 30px -10px rgba(139, 92, 246, .75), inset 0 1px 0 rgba(255,255,255,.25);
    transition: background-position .5s ease, transform .15s ease, box-shadow .2s ease !important;
}
[data-testid="stBaseButton-primary"]:hover { background-position: 100% 0 !important; transform: translateY(-1px);
    box-shadow: 0 16px 40px -12px rgba(236, 72, 153, .7), inset 0 1px 0 rgba(255,255,255,.3); }
[data-testid="stBaseButton-primary"]:active { transform: translateY(0); }
[data-testid="stBaseButton-secondary"] { background: rgba(255,255,255,.04) !important; border: 1px solid var(--borda) !important; border-radius: 12px !important; }
[data-testid="stBaseButton-secondary"]:hover { border-color: var(--borda-forte) !important; }

/* ---------- progresso, alertas, expander, tabela ---------- */
[data-testid="stProgress"] div[role="progressbar"] > div { background: rgba(255,255,255,.06) !important; border-radius: 999px; }
[data-testid="stProgress"] div[role="progressbar"] > div > div { background: var(--degrade) !important; border-radius: 999px; }
[data-testid="stAlert"] { border-radius: 14px; backdrop-filter: blur(10px); border: 1px solid var(--borda); }
[data-testid="stExpander"] details { background: var(--vidro); border: 1px solid var(--borda) !important; border-radius: 14px; }
[data-testid="stDataFrame"] { border: 1px solid var(--borda); border-radius: 14px; overflow: hidden; }

/* ---------- métricas do lote ---------- */
.metricas { display: grid; grid-template-columns: repeat(4, 1fr); gap: .8rem; margin: .4rem 0 1.1rem; }
.metrica { padding: 1rem 1.15rem; border-radius: 16px; background: var(--vidro); border: 1px solid var(--borda); }
.metrica small { display: block; color: var(--texto-2); font-size: .74rem; text-transform: uppercase; letter-spacing: .12em; }
.metrica strong { display: block; margin-top: .35rem; font: 600 1.55rem 'Space Grotesk', sans-serif;
    background: var(--degrade); -webkit-background-clip: text; background-clip: text; color: transparent; }
.metrica.erro strong { background: none; color: var(--erro); }
@media (max-width: 760px) { .metricas { grid-template-columns: repeat(2, 1fr); } }

/* ---------- status do ambiente (sidebar) ---------- */
.amb { display: flex; flex-direction: column; gap: .4rem; }
.amb-item { display: flex; align-items: center; gap: .6rem; font-size: .85rem; padding: .5rem .7rem;
    border-radius: 10px; background: rgba(255,255,255,.03); border: 1px solid var(--borda); }
.amb-item i { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.amb-item.ok i { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.amb-item.alerta i { background: var(--alerta); box-shadow: 0 0 8px var(--alerta); }
.amb-item.erro i { background: var(--erro); box-shadow: 0 0 8px var(--erro); }
.amb-item em { margin-left: auto; font-style: normal; color: var(--texto-2); font-size: .74rem; }
.detalhe-int { font-size: .78rem; color: var(--texto-2); padding: .55rem .7rem; border-radius: 10px;
    background: rgba(139,92,246,.08); border: 1px solid rgba(139,92,246,.2); font-family: 'JetBrains Mono', monospace; }
</style>
"""


def html(trecho: str) -> None:
    """Renderiza HTML cru. Remove a indentação: no Markdown, linha com 4+ espaços vira bloco de código."""
    st.markdown("\n".join(linha.strip() for linha in trecho.splitlines() if linha.strip()), unsafe_allow_html=True)


def aplicar_estilo() -> None:
    html(ESTILO)


def hero() -> None:
    html(
        """
        <div class="hero">
            <span class="hero-tag"><i></i>Processamento local · nada sai da máquina</span>
            <h1>DeAI · Painel de processamento</h1>
            <p>Remove metadados de proveniência e aplica o pipeline de pixels escolhido na barra lateral
            a todas as imagens do lote — tudo em memória, entregue num único <b>.zip</b>.</p>
            <div class="hero-chips">
                <span class="chip"><b>EXIF</b> · <b>XMP</b> · <b>C2PA</b></span>
                <span class="chip">Lote ilimitado</span>
                <span class="chip">Relatório CSV com PSNR</span>
                <span class="chip">PNG · JPG · WEBP</span>
            </div>
        </div>
        """
    )


def passos_iniciais() -> None:
    html(
        """
        <div class="passos">
            <div class="passo"><div class="num">1</div><div class="tit">Envie as imagens</div>
                <p>Arraste quantos arquivos quiser para a área acima.</p></div>
            <div class="passo"><div class="num">2</div><div class="tit">Ajuste a intensidade</div>
                <p>Escolha o perfil e a emulação de sensor na barra lateral.</p></div>
            <div class="passo"><div class="num">3</div><div class="tit">Baixe o .zip</div>
                <p>Imagens processadas + relatorio.csv com métricas por arquivo.</p></div>
        </div>
        """
    )


def metricas_lote(lote: dict) -> None:
    psnrs = [l["psnr_db"] for l in lote["linhas"] if isinstance(l["psnr_db"], (int, float)) and l["psnr_db"] != float("inf")]
    psnr_medio = f"{sum(psnrs) / len(psnrs):.1f} dB" if psnrs else "—"
    classe_falhas = "metrica erro" if lote["falhas"] else "metrica"
    html(
        f"""
        <div class="metricas">
            <div class="metrica"><small>Processadas</small><strong>{lote['ok']}/{lote['total']}</strong></div>
            <div class="{classe_falhas}"><small>Falhas</small><strong>{len(lote['falhas'])}</strong></div>
            <div class="metrica"><small>PSNR médio</small><strong>{psnr_medio}</strong></div>
            <div class="metrica"><small>Tempo total</small><strong>{lote['duracao']:.1f} s</strong></div>
        </div>
        """
    )


def barra_lateral(ambiente: dict) -> tuple[str, bool, dict | None]:
    """Desenha a barra lateral e devolve (strength, somente_metadados, parâmetros da emulação ou None)."""
    with st.sidebar:
        st.header("Configuração")

        rotulo = st.select_slider("Intensidade", options=list(INTENSIDADES.keys()), value="Média (Padrão)")
        strength = INTENSIDADES[rotulo]
        st.markdown(f'<div class="detalhe-int">{DETALHES_INTENSIDADE[strength]}</div>', unsafe_allow_html=True)

        # O modo --no-metadata do script depende 100% do ExifTool.
        somente_metadados = st.toggle(
            "Só remover metadados",
            value=False,
            disabled=not ambiente["exiftool"],
            help="Mantém os pixels intactos e remove apenas EXIF/XMP/C2PA. Requer ExifTool instalado.",
        )

        st.divider()
        st.subheader("Emulação de sensor óptico")
        emular = st.toggle(
            "Aplicar após o script",
            value=True,
            disabled=somente_metadados,
            help="Aberração cromática, reamostragem bilinear→bicúbica, nitidez nas bordas e grão monocromático.",
        )
        pos_processamento = None
        if emular and not somente_metadados:
            pos_processamento = {
                "deslocamento_ca": st.select_slider("Aberração cromática (px)", options=[1, 2], value=1),
                "escala_reamostragem": st.slider("Reamostragem (%)", 60, 95, 80, step=5) / 100,
                "sigma_grao": st.slider("Grão monocromático (σ)", 0.0, 8.0, 3.0, step=0.5),
            }

        st.divider()
        st.subheader("Ambiente")
        itens = [
            ("ok" if ambiente["script"] else "erro", f"scripts/{SCRIPT_NAME}", "pronto" if ambiente["script"] else "ausente"),
            ("ok" if ambiente["libs_subprocesso"] else "erro", "Pillow + NumPy", "ok" if ambiente["libs_subprocesso"] else "faltando"),
            ("ok" if ambiente["exiftool"] else "alerta", "ExifTool", "ok" if ambiente["exiftool"] else "opcional"),
        ]
        st.markdown(
            '<div class="amb">'
            + "".join(f'<div class="amb-item {classe}"><i></i>{nome}<em>{estado}</em></div>' for classe, nome, estado in itens)
            + "</div>",
            unsafe_allow_html=True,
        )
        if ambiente["exiftool"]:
            st.caption(f"ExifTool: `{ambiente['exiftool']}`")
        if not ambiente["exiftool"]:
            st.caption(
                "Sem ExifTool o pipeline completo ainda sai sem EXIF/C2PA (a imagem é reconstruída pixel a pixel), "
                "mas o modo só-metadados fica indisponível. Windows: `winget install OliverBetz.ExifTool`."
            )
        st.caption(f"Python: `{sys.executable}`")

        st.divider()
        st.subheader("⚡ Nuvem Supabase")
        salvar_sb = st.toggle(
            "Ativar Supabase",
            value=st.session_state.get("usar_supabase", False),
            help="Envia as imagens processadas para o Supabase Storage e salva histórico no Postgres.",
        )
        st.session_state["usar_supabase"] = salvar_sb
        sb_mgr = None
        if salvar_sb:
            with st.expander("Credenciais Supabase", expanded=True):
                padrao_sb = obter_supabase()
                url_inicial = st.session_state.get("sb_url") or (padrao_sb.url if padrao_sb else "")
                key_inicial = st.session_state.get("sb_key") or (padrao_sb.key if padrao_sb else "")
                bucket_inicial = st.session_state.get("sb_bucket") or (padrao_sb.bucket if padrao_sb else "deai-images")

                sb_url = st.text_input("Supabase Project URL", value=url_inicial, placeholder="https://xyz.supabase.co")
                sb_key = st.text_input("Chave API (anon/service)", value=key_inicial, type="password")
                sb_bucket = st.text_input("Bucket do Storage", value=bucket_inicial)

                st.session_state["sb_url"] = sb_url
                st.session_state["sb_key"] = sb_key
                st.session_state["sb_bucket"] = sb_bucket

                sb_mgr = obter_supabase(sb_url, sb_key, sb_bucket)
                if sb_mgr and sb_mgr.is_configured:
                    ok_conn, msg_conn = sb_mgr.testar_conexao()
                    if ok_conn:
                        st.caption("🟢 Conectado ao Supabase")
                    else:
                        st.caption(f"🟡 {msg_conn}")
                else:
                    st.caption("ℹ️ Preencha a URL e a Chave para ativar o envio automático.")

    return strength, somente_metadados, pos_processamento, sb_mgr


def main() -> None:
    st.set_page_config(page_title="DeAI · Painel", page_icon="◐", layout="wide", initial_sidebar_state="expanded")
    aplicar_estilo()
    hero()

    # Falhas de ambiente que impedem qualquer processamento: avisa e para aqui.
    if not DEPENDENCIAS_OK:
        st.error(f"Biblioteca ausente no painel: {ERRO_IMPORT}. Rode `python -m pip install -r requirements.txt`.")
        st.stop()

    ambiente = verificar_ambiente()
    strength, somente_metadados, pos_processamento, sb_mgr = barra_lateral(ambiente)

    if not ambiente["script"]:
        st.error(f"Script não encontrado em `{CAMINHO_SCRIPT}`. Confira a variável `SCRIPT_NAME` no topo do app.py.")
        st.stop()
    if not ambiente["libs_subprocesso"]:
        st.error(f"O Python do subprocesso não importa Pillow/NumPy: {ambiente['erro_libs']}")
        st.stop()

    arquivos = st.file_uploader(
        "Imagens",
        type=TIPOS_ACEITOS,
        accept_multiple_files=True,
        help="Arraste ou selecione várias imagens de uma vez (PNG, JPG, JPEG, WEBP).",
    )

    if not arquivos:
        st.session_state.pop("lote", None)
        passos_iniciais()
        return

    # Se a seleção de arquivos mudar, o ZIP anterior deixa de valer.
    assinatura = tuple(arquivo.file_id for arquivo in arquivos)
    lote = st.session_state.get("lote")
    if lote and lote["assinatura"] != assinatura:
        st.session_state.pop("lote", None)
        lote = None

    tamanho_total_mb = sum(arquivo.size for arquivo in arquivos) / (1024 * 1024)
    st.caption(f"{len(arquivos)} arquivo(s) selecionado(s) · {tamanho_total_mb:.1f} MB")

    acao = "Remover metadados" if somente_metadados else f"Processar ({strength})"
    if st.button(f"{acao} · {len(arquivos)} imagem(ns)", type="primary", width="stretch"):
        mensagem = st.empty()
        barra = st.progress(0.0)
        with st.expander("Log do arquivo em processamento", expanded=False):
            painel_log = st.empty()

        try:
            lote = processar_lote(
                arquivos, strength, somente_metadados, pos_processamento, barra, mensagem, painel_log, supabase_mgr=sb_mgr
            )
        except MemoryError:
            # O ZIP e as imagens ficam na RAM: um lote gigante pode não caber.
            mensagem.empty()
            st.error("Memória insuficiente para montar o ZIP deste lote. Divida em lotes menores.")
            return
        except (OSError, zipfile.BadZipFile) as erro:
            mensagem.empty()
            st.error(f"Falha ao montar o lote: {erro}")
            return

        lote["assinatura"] = assinatura
        lote["nome_zip"] = f"lote_processado_{datetime.now():%Y%m%d_%H%M%S}.zip"
        # Guardado na sessão para sobreviver aos reruns do Streamlit.
        st.session_state["lote"] = lote

    if lote is None:
        return

    # ------------------------------------------------------------- resultado do lote
    metricas_lote(lote)
    if lote["ok"] == 0:
        st.error("Nenhuma imagem foi processada. Veja os erros abaixo.")
    elif lote["falhas"]:
        st.warning(
            f"{lote['ok']} de {lote['total']} imagens processadas em {lote['duracao']:.1f} s. "
            f"{len(lote['falhas'])} falharam e ficaram fora do ZIP."
        )
    else:
        st.success(
            f"Lote concluído: {lote['total']} imagens em {lote['duracao']:.1f} s. "
            "Nenhum arquivo temporário ficou no disco; o ZIP foi montado na memória."
        )

    if lote["ok"]:
        st.download_button(
            f"Baixar lote (.zip · {len(lote['zip']) / (1024 * 1024):.1f} MB)",
            data=lote["zip"],
            file_name=lote["nome_zip"],
            mime="application/zip",
            type="primary",
            width="stretch",
            on_click="ignore",  # não dispara rerun ao baixar
        )

    if lote.get("itens_processados"):
        with st.expander(f"📥 Baixar imagens individualmente ({len(lote['itens_processados'])})", expanded=True):
            st.markdown(
                "<p style='color: var(--texto-2); font-size: 0.88rem; margin-bottom: 0.8rem;'>"
                "Clique no botão de cada imagem para baixar o arquivo individual correspondente:"
                "</p>",
                unsafe_allow_html=True,
            )
            itens = lote["itens_processados"]
            num_cols = min(3, max(1, len(itens)))
            for i in range(0, len(itens), num_cols):
                cols = st.columns(num_cols)
                for c_idx, item in enumerate(itens[i : i + num_cols]):
                    with cols[c_idx]:
                        st.image(item["bytes"], use_container_width=True)
                        rotulo_psnr = f" · PSNR {item['psnr']} dB" if item.get("psnr") else ""
                        st.caption(f"**{item['nome']}**\n{item['kb']} KB{rotulo_psnr}")
                        st.download_button(
                            label=f"⬇️ Baixar imagem",
                            data=item["bytes"],
                            file_name=item["nome"],
                            mime=item["mime"],
                            key=f"btn_dl_{item['nome']}_{i}_{c_idx}",
                            use_container_width=True,
                            on_click="ignore",
                        )
                        if item.get("url_publica"):
                            st.link_button("🌐 Abrir no Supabase", item["url_publica"], use_container_width=True)

    st.dataframe(
        lote["linhas"],
        hide_index=True,
        width="stretch",
        column_config={
            "arquivo": "Arquivo",
            "status": "Status",
            "saida": "Nome no ZIP",
            "modo": "Modo",
            "url_publica": st.column_config.LinkColumn("Link Supabase", display_text="Abrir imagem"),
            "kb_antes": st.column_config.NumberColumn("KB antes", format="%.1f"),
            "kb_depois": st.column_config.NumberColumn("KB depois", format="%.1f"),
            "psnr_db": st.column_config.NumberColumn(
                "PSNR (dB)", format="%.1f", help="Acima de ~40 dB a diferença é praticamente invisível; inf = pixels idênticos."
            ),
            "metadados_antes": "Metadados antes",
            "metadados_depois": "Metadados depois",
            "segundos": st.column_config.NumberColumn("Tempo (s)", format="%.1f"),
        },
    )
    st.caption("O ZIP inclui um relatorio.csv com esta mesma tabela.")

    if lote["falhas"]:
        with st.expander(f"Erros ({len(lote['falhas'])})", expanded=lote["ok"] == 0):
            for falha in lote["falhas"]:
                st.markdown(f"**{falha['arquivo']}** — {falha['motivo']}")
                st.code(falha["log"] or "(sem saída)", language="text")


if __name__ == "__main__":
    main()
