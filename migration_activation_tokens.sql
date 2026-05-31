-- =============================================================
-- Migration: tabela activation_tokens
-- Usada pelo fluxo "Ativar Online" (sem arquivo .key)
-- Executar no Neon (psql ou dashboard SQL Editor)
-- =============================================================

CREATE TABLE IF NOT EXISTS activation_tokens (
    id              SERIAL PRIMARY KEY,
    cnpj            VARCHAR(14)   NOT NULL,
    token_hash      CHAR(64)      NOT NULL UNIQUE,  -- SHA-256 hex do raw token
    criado_em       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    expira_em       TIMESTAMPTZ   NOT NULL,
    usado_em        TIMESTAMPTZ,
    device_id_usado VARCHAR(64),
    gerado_por      VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_act_tokens_cnpj
    ON activation_tokens(cnpj);

-- Índice parcial: apenas tokens ainda não usados (útil para lookup rápido)
CREATE INDEX IF NOT EXISTS idx_act_tokens_validos
    ON activation_tokens(cnpj, expira_em)
    WHERE usado_em IS NULL;
