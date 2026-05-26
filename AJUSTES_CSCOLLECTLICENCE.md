# Ajuste do CSCollectLicence - Implementação de api_authorization e api_database_url

## 🎯 Objetivo

Garantir que o arquivo `.key` (licença) contenha os campos `api_authorization` e `api_database_url`, permitindo que:
- **CSCollectManager** leia esses valores localmente do `.key`
- **CSCollect** tenha acesso aos dados de autenticação sem consultar banco de dados
- **CSCollectAPI** possa validar corretamente a licença

---

## 📝 Alterações Realizadas

### 1. **CSCollectLicence/licenca.py**

#### Modificação 1: Função `gerar_licenca` (linha 138)

**Antes:**
```python
def gerar_licenca(cnpjs, ids_celular, validade, nome_cliente, sql_servidor, sql_banco):
```

**Depois:**
```python
def gerar_licenca(cnpjs, ids_celular, validade, nome_cliente, sql_servidor, sql_banco, 
                 api_authorization="", api_database_url=""):
```

**Impacto:** Os dois novos parâmetros são incluídos no payload do token gerado.

---

#### Modificação 2: Função `salvar_licenca` (linha 260)

**Antes:**
```python
out = {
    "cnpjs": payload_meta.get("cnpjs") or payload_meta.get("cnpj") or [],
    "ids": payload_meta.get("ids") or payload_meta.get("ids_celular") or [],
    "token": token,
    "validade": payload_meta.get("validade"),
    "api_url": payload_meta.get("api_url") or "",
    "nome_cliente": payload_meta.get("nome_cliente") or "",
    "sql_servidor": payload_meta.get("sql_servidor") or "",
    "sql_banco": payload_meta.get("sql_banco") or "",
}
```

**Depois:**
```python
out = {
    "cnpjs": payload_meta.get("cnpjs") or payload_meta.get("cnpj") or [],
    "ids": payload_meta.get("ids") or payload_meta.get("ids_celular") or [],
    "token": token,
    "validade": payload_meta.get("validade"),
    "api_url": payload_meta.get("api_url") or "",
    "nome_cliente": payload_meta.get("nome_cliente") or "",
    "sql_servidor": payload_meta.get("sql_servidor") or "",
    "sql_banco": payload_meta.get("sql_banco") or "",
    "api_authorization": payload_meta.get("api_authorization") or "",
    "api_database_url": payload_meta.get("api_database_url") or "",
}
```

**Impacto:** O arquivo `.key` agora inclui os dois campos na saída JSON.

---

#### Modificação 3: Função `carregar_licenca_de_arquivo` (linha 300)

**Adicionado:** Leitura dos campos ao carregar JSON do `.key`:
```python
'api_authorization': doc.get('api_authorization', ''),
'api_database_url': doc.get('api_database_url', ''),
```

**Impacto:** O payload retornado pela função inclui esses campos para leitura local.

---

### 2. **CSCollectLicence/gui_licenca.py**

#### Modificação: Adicionar campos de entrada na UI

**Adicionado:**
- Campo de entrada `api_authorization_edit` (QLineEdit, máx 100 caracteres)
- Campo de entrada `api_database_url_edit` (QLineEdit, máx 100 caracteres)
- Labels descritivos: "API Authorization Token" e "API Database URL"

**Comportamentos:**
- Campos são preenchidos ao carregar arquivo existente
- Valores são coletados ao gerar/salvar licença
- Valores opcionais (vazios por padrão)

---

### 3. **Arquivo `.key` Atualizado**

O arquivo `Licenca_CSCollectManager_LOCAL_ELETRÔNICA.key` agora contém:

```json
{
  "cnpjs": ["65381113000120"],
  "ids": ["a3e9e3a0a4659652"],
  "token": "eyJjbnBqcyI6WyI2NTM4MTExMzAwMDEyMCJdLCJpZHNfY2VsdWxhciI6WyJhM2U5ZTNhMGE0NjU5NjUyIl0sInZhbGlkYWRlIjoiMjAyNi0wNS0yOSIsIm5vbWVfY2xpZW50ZSI6IkxPQ0FMIEVMRVRSw5ROSUNBIExJTUlUQURBIiwic3FsX3NlcnZpZG9yIjoiQ0VPU09GVC1TRVJWMSIsInNxbF9iYW5jbyI6IkxPQ0FMIiwiZ2VyYWRvX2VtIjoiMjAyNi0wNS0yNlQxMjo0MjowMy0wMzowMCIsImFwaV9hdXRob3JpemF0aW9uIjoiVGZKcFNvVVBLeGMwWStGV24rZ3JlIn0.tCkBv4PrOs5DWEk4v_gLQdw09ahaotU03t11L3IA3PY",
  "validade": "2026-05-29",
  "api_url": "https://cscollectapi.onrender.com",
  "nome_cliente": "LOCAL ELETRÔNICA LIMITADA",
  "sql_servidor": "CEOSOFT-SERV1",
  "sql_banco": "LOCAL",
  "api_authorization": "TfJpSoUPKxc0Y+FWn+gre",
  "api_database_url": ""
}
```

**Novos campos:**
- ✅ `api_authorization`: Token para autenticar requisições da API (agora presente!)
- ✅ `api_database_url`: URL do banco de dados do cliente (cache local)

---

## 📦 Distribuição dos Arquivos Atualizados

O arquivo `.key` foi copiado para:
- ✅ `C:\Users\alex.CEOSOFTWAREAD\Documents\Python\VSCode\CSCollectAPI\`
- ✅ `C:\Users\alex.CEOSOFTWAREAD\Documents\Python\VSCode\CSCollectManager\`
- ✅ `C:\Users\alex.CEOSOFTWAREAD\Documents\Python\VSCode\CSCollect\`

---

## 🔍 Como Verificar se Funcionou

### 1. **CSCollectManager**

Ao abrir o CSCollectManager e validar a licença:
- Deve ler o `api_authorization` do arquivo `.key`
- Não deve mais exibir: "Token de autorização da API não encontrado na licença"

### 2. **CSCollect**

Ao abrir o aplicativo mobile CSCollect:
- Pode acessar `api_authorization` e `api_database_url` localmente
- Não precisa fazer requisição para cada validação

### 3. **CSCollectAPI**

O endpoint `/validar-licenca` agora retorna corretamente:
```json
{
  "ok": true,
  "api_authorization": "TfJpSoUPKxc0Y+FWn+gre",
  "api_database_url": "",
  ...
}
```

---

## 🔄 Fluxo de Geração de Novas Licenças

A partir de agora, quando gerar uma nova licença no **CSCollectLicence**:

1. **GUI** coleta `api_authorization` e `api_database_url`
2. **gerar_licenca()** inclui esses valores no token (se fornecidos)
3. **salvar_licenca()** escreve os valores no arquivo JSON `.key`
4. **CSCollectManager** e **CSCollect** leem os valores localmente
5. **CSCollectAPI** valida e retorna os dados ao cliente

---

## ✅ Checklist de Implementação

- [x] Modificar `gerar_licenca()` para aceitar novos parâmetros
- [x] Modificar `salvar_licenca()` para gravar campos no `.key`
- [x] Modificar `carregar_licenca_de_arquivo()` para ler campos
- [x] Atualizar GUI para coletar os campos
- [x] Regenerar arquivo `.key` com novos campos
- [x] Distribuir arquivo `.key` atualizado para todos os projetos
- [x] Testar integração entre componentes

---

## 📌 Notas Importantes

1. **Compatibilidade retroativa:** Parâmetros são opcionais (valores padrão vazios)
2. **Segurança:** `api_authorization` é armazenado criptografado no banco Neon
3. **Cache local:** O `.key` serves como fallback se banco não estiver acessível
4. **Geração de tokens:** Não alterada - tokens continuam assinados normalmente

