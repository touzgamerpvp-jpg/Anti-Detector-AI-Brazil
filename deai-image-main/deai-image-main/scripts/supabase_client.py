import os
import json
import requests
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

class SupabaseManager:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None, bucket: str = "deai-images"):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
        self.bucket = bucket or os.environ.get("SUPABASE_BUCKET", "deai-images")

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self, content_type: str = "application/json") -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": content_type,
        }

    def testar_conexao(self) -> Tuple[bool, str]:
        """Testa a conectividade com o Supabase."""
        if not self.is_configured:
            return False, "URL ou Chave do Supabase não configuradas."
        try:
            resp = requests.get(
                f"{self.url}/rest/v1/historico_deai?limit=1",
                headers=self._headers(),
                timeout=5
            )
            if resp.status_code in [200, 206]:
                return True, "Conexão com banco Supabase OK."
            elif resp.status_code == 404:
                return False, "Tabela 'historico_deai' não encontrada. Execute o supabase_schema.sql no SQL Editor."
            else:
                return False, f"Erro de autenticação/acesso: HTTP {resp.status_code} - {resp.text}"
        except Exception as e:
            return False, f"Falha de conexão: {str(e)}"

    def upload_imagem(self, nome_arquivo: str, bytes_conteudo: bytes, mime_type: str = "image/jpeg") -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Envia uma imagem para o bucket do Supabase Storage.
        Retorna: (sucesso, storage_path, url_publica)
        """
        if not self.is_configured:
            return False, None, None

        pasta_data = datetime.now().strftime("%Y%m%d")
        storage_path = f"{pasta_data}/{nome_arquivo}"
        endpoint = f"{self.url}/storage/v1/object/{self.bucket}/{storage_path}"

        headers = self._headers(content_type=mime_type)
        headers["x-upsert"] = "true"

        try:
            resp = requests.post(endpoint, headers=headers, data=bytes_conteudo, timeout=30)
            if resp.status_code in [200, 201]:
                url_publica = f"{self.url}/storage/v1/object/public/{self.bucket}/{storage_path}"
                return True, storage_path, url_publica
            else:
                return False, None, f"Erro HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, None, str(e)

    def salvar_historico(self, dados: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Grava um registro na tabela historico_deai.
        """
        if not self.is_configured:
            return False, "Supabase não configurado"

        endpoint = f"{self.url}/rest/v1/historico_deai"
        headers = self._headers()
        headers["Prefer"] = "return=minimal"

        payload = {
            "arquivo_original": str(dados.get("arquivo", "")),
            "arquivo_saida": str(dados.get("saida", "")),
            "status": str(dados.get("status", "")),
            "modo": str(dados.get("modo", "")),
            "kb_antes": dados.get("kb_antes"),
            "kb_depois": dados.get("kb_depois"),
            "psnr_db": dados.get("psnr_db"),
            "metadados_antes": str(dados.get("metadados_antes", "")),
            "metadados_depois": str(dados.get("metadados_depois", "")),
            "segundos": dados.get("segundos"),
            "storage_path": dados.get("storage_path"),
            "url_publica": dados.get("url_publica"),
        }

        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=10)
            if resp.status_code in [200, 201]:
                return True, "Registro salvo no Supabase."
            else:
                return False, f"HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, str(e)
