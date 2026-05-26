# Diagnóstico: Erro "Token de autorização da API não encontrado na licença"

## Problema Identificado

A imagem mostra uma mensagem de erro no **CSCollectManager** ao tentar baixar uma carga da API:

```
❌ Erro ao baixar carga
Token de autorização da API não encontrado na licença.

Verifique se a licença está ativada e contém o campo 'api_authorization' no arquivo .key.
Contate o suporte para reativar a licença com as credenciais de API.
```

## Causa

O erro ocorre na rota `/validar-licenca` da API ([api.py](api.py#L665-L728)).

Quando o CSCollectManager valida a licença (enviando CNPJ + device_id), a API retorna este JSON:

```json
{
  "ok": true,
  "api_authorization": ""  ← VAZIO!
}
```

O campo `api_authorization` está **vazio ou NULL** no banco de dados Neon, na tabela `clientes`.

## Solução

Você precisa **atualizar o banco de dados Neon** para preencher o campo `api_authorization` com um token válido.

### Passo 1: Conectar ao Neon e executar SQL

Use uma ferramenta SQL (pgAdmin, DBeaver, ou CLI `psql`) para conectar ao banco Neon com a URL em `DATABASE_URL`.

### Passo 2: Localizar o cliente

Identifique o CNPJ do cliente que está com o erro (visível na imagem: "Usuário: 043").

### Passo 3: Atualizar o campo `api_authorization`

Execute este SQL para gerar e atualizar um token:

```sql
UPDATE clientes 
SET api_authorization = '6b2876c8d1a4e9f5b3c2e7d1a9f4c6b2'  -- Gere um token válido
WHERE cnpj = '<CNPJ_DO_CLIENTE>';
```

**Para gerar um token válido**, você pode usar:
- Um UUID aleatório: `SELECT gen_random_uuid()::text`
- Um hash: Um valor hexadecimal de 32 caracteres (exemplo: resultado de MD5)
- Mesmo valor do campo `token` da tabela `clientes`

### Passo 4: Verificar se funcionou

Execute esta query para confirmar:

```sql
SELECT cnpj, api_authorization FROM clientes WHERE cnpj = '<CNPJ_DO_CLIENTE>';
```

O resultado deve mostrar `api_authorization` **não vazio**.

## Alternativa: Atualizar via aplicação

Se preferir automatizar, você pode adicionar uma rota de administração na API:

```python
@app.put("/admin/cliente/{cnpj}/api-token")
def atualizar_api_token(cnpj: str, token: str, authorization: str = Header(...)):
    """Atualiza o api_authorization de um cliente"""
    verificar_token(authorization)  # Valida token de admin
    
    db = Session()
    try:
        db.execute(
            text("UPDATE clientes SET api_authorization = :token WHERE cnpj = :cnpj"),
            {"token": token, "cnpj": cnpj}
        )
        db.commit()
        return {"ok": True, "mensagem": f"Token atualizado para {cnpj}"}
    finally:
        db.close()
```

## Campos relacionados na tabela `clientes`

| Campo | Descrição | Status |
|-------|-----------|--------|
| `token` | Token principal do cliente (usado no .key) | ✅ Preenchido |
| `api_authorization` | Token para autorizar requisições da API | ❌ **Vazio/NULL** |
| `api_database_url` | URL do banco de dados do cliente | Verificar |

Ambos devem estar preenchidos para a API funcionar corretamente.

## Rastreamento no código

A validação ocorre aqui:

**API** ([api.py](api.py#L695-L699)):
```python
'api_authorization': str(db_api_auth) if db_api_auth else '',  # Retorna vazio
```

**Aplicação** (CSCollectManager):
- Se `api_authorization` vazio → Exibe o erro da imagem
- Usada para autenticar requisições ao banco de dados do cliente

## Próximas ações

1. ✅ Atualize o banco com um token válido para `api_authorization`
2. ✅ Teste a validação novamente no CSCollectManager
3. ✅ Se persistir, verifique se o CNPJ está correto na consulta
