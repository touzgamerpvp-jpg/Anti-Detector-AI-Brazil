-- ========================================================
-- Schema Supabase para o DeAI Image Processing
-- Execute este script no SQL Editor do seu Dashboard Supabase
-- ========================================================

-- 1. Criação do Bucket de Storage (deai-images) se não existir
INSERT INTO storage.buckets (id, name, public)
VALUES ('deai-images', 'deai-images', true)
ON CONFLICT (id) DO UPDATE SET public = true;

-- Permite leitura pública de todas as imagens no bucket 'deai-images'
CREATE POLICY "Leitura pública de imagens"
ON storage.objects FOR SELECT
USING (bucket_id = 'deai-images');

-- Permite inserção de imagens via API anon / autenticada
CREATE POLICY "Upload de imagens processadas"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'deai-images');

-- Necessária para o upload com "x-upsert: true" sobrescrever um arquivo de mesmo nome
CREATE POLICY "Sobrescrever imagens processadas"
ON storage.objects FOR UPDATE
USING (bucket_id = 'deai-images')
WITH CHECK (bucket_id = 'deai-images');

-- 2. Tabela de histórico de processamentos
CREATE TABLE IF NOT EXISTS public.historico_deai (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT now(),
    arquivo_original TEXT NOT NULL,
    arquivo_saida TEXT NOT NULL,
    status TEXT NOT NULL,
    modo TEXT,
    kb_antes NUMERIC,
    kb_depois NUMERIC,
    psnr_db NUMERIC,
    metadados_antes TEXT,
    metadados_depois TEXT,
    segundos NUMERIC,
    storage_path TEXT,
    url_publica TEXT
);

-- Habilitar Row Level Security (RLS)
ALTER TABLE public.historico_deai ENABLE ROW LEVEL SECURITY;

-- Permissões para API pública/anon
CREATE POLICY "Permitir inserção de histórico"
ON public.historico_deai FOR INSERT
WITH CHECK (true);

CREATE POLICY "Permitir leitura de histórico"
ON public.historico_deai FOR SELECT
USING (true);
