# 🚀 Guia de Deploy & Integração: DeAI Image Studio

Este guia explica como colocar seu site no ar na nuvem funcionando **exatamente como no seu computador**, com integração completa ao **Supabase (Storage & Banco de Dados)** e publicação via **Streamlit Cloud** e/ou **Netlify**.

---

## 🏗️ Como a Arquitetura Funciona

| Componente | Função | Onde Hospedar |
| :--- | :--- | :--- |
| **Painel DeAI** | Processamento de imagens em Python (Pillow, NumPy, ExifTool, WebSockets) | **Streamlit Community Cloud** (100% Gratuito) |
| **Armazenamento & Banco** | Guarda as imagens no Storage e o histórico em tabela Postgres | **Supabase** (100% Gratuito) |
| **Domínio / Portal** | (Opcional) Serve a página inicial ou domínio personalizado | **Netlify** (100% Gratuito) |

---

## ⚡ Passo 1: Configurar o Supabase (Storage & DB)

1. Acesse [supabase.com](https://supabase.com) e crie uma conta gratuita (ou faça login).
2. Crie um **Novo Projeto**.
3. No menu lateral esquerdo, clique em **SQL Editor** (ícone de terminal `>_`).
4. Abra o arquivo [`supabase_schema.sql`](./supabase_schema.sql) deste projeto, copie todo o conteúdo e cole no SQL Editor do Supabase.
5. Clique em **Run** (Executar).
   - Isso criará automaticamente o bucket `deai-images` no Storage público e a tabela `historico_deai` com as permissões de acesso liberadas.
6. Vá em **Project Settings** (ícone de engrenagem) > **API**:
   - Copie a **Project URL** (ex: `https://abcdefgh.supabase.co`).
   - Copie a chave **anon / public** ou **service_role**.

---

## 🌐 Passo 2: Deploy no Streamlit Cloud (Recomendado)

O **Streamlit Community Cloud** é a plataforma oficial e gratuita para rodar aplicações Streamlit com suporte completo a WebSockets e processos contínuos de Python.

### 2.1. Subir para o GitHub
No seu computador, abra o terminal na pasta do projeto e execute:
```bash
git init
git add .
git commit -m "feat: deploy ready com download individual e supabase"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git
git push -u origin main
```

### 2.2. Criar o App no Streamlit Cloud
1. Acesse [share.streamlit.io](https://share.streamlit.io) e entre com sua conta do GitHub.
2. Clique no botão azul **"New app"**.
3. Preencha as configurações:
   - **Repository:** `SEU_USUARIO/SEU_REPOSITORIO`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Clique em **"Advanced settings..."** (Configurações Avançadas):
   - Na caixa **Secrets**, cole as credenciais do seu Supabase:
     ```toml
     [supabase]
     url = "https://abcdefgh.supabase.co"
     key = "sua-chave-anon-ou-service-role-aqui"
     bucket = "deai-images"
     ```
5. Clique em **"Deploy!"**.
6. Aguarde cerca de 1 a 2 minutos:
   - O Streamlit lerá o arquivo `packages.txt` e instalará o **ExifTool** automaticamente no servidor Linux.
   - O Streamlit instalará os requisitos do `requirements.txt`.
   - Seu site estará no ar em: `https://seu-projeto.streamlit.app`!

---

## 🪐 Passo 3: Publicar no Netlify (Opcional)

Se você deseja que o seu link seja no **Netlify** (ex: `https://meu-app.netlify.app`):

1. Abra o arquivo [`netlify-deploy/public/index.html`](./netlify-deploy/public/index.html).
2. Na linha 61, altere o valor da variável `STREAMLIT_APP_URL` para o endereço do seu app gerado no Streamlit Cloud:
   ```javascript
   const STREAMLIT_APP_URL = "https://seu-projeto.streamlit.app";
   ```
3. Acesse [app.netlify.com](https://app.netlify.com).
4. Arraste a pasta `netlify-deploy` diretamente para a área **"Deploy manually / Netlify Drop"** OU conecte o repositório escolhendo a pasta `netlify-deploy` como diretório base e `public` como pasta de publicação.
5. Pronto! Seu domínio Netlify agora exibirá o app em tela cheia com visual profissional.

---

## 📥 Como Baixar Imagens Individualmente Após o Lote

1. Abra o painel (localmente ou na nuvem).
2. Envie suas imagens no seletor de arquivos.
3. Se quiser salvar na nuvem, ative a opção **"⚡ Nuvem Supabase"** na barra lateral.
4. Clique em **Processar**.
5. Ao concluir o processamento:
   - Você verá o botão tradicional **"Baixar lote (.zip)"** para salvar tudo compactado.
   - Logo abaixo, abrirá a seção **"📥 Baixar imagens individualmente"**:
     - Cada imagem é exibida com miniatura, tamanho final em KB e nível de PSNR (dB).
     - Botão individual **"⬇️ Baixar imagem"** para salvar direto no seu dispositivo.
     - Botão **"🌐 Abrir no Supabase"** caso a integração com o Supabase esteja ligada.
