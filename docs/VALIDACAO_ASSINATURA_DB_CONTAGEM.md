# Validação do arquivo `.db` de contagens — guia para o ERP retaguarda (VB6)

Este documento é para o programador do ERP retaguarda: explica como verificar,
**antes de importar**, se o arquivo `.db` exportado pelo LogScan (app mobile
CSCollect) é genuíno e não foi alterado manualmente.

---

## 1. Contexto

O LogScan exporta as contagens de estoque em um `.zip` contendo:

```
CONTAGEM_1_043_65381113000120_070520261714.zip
├── CONTAGEM_1_043_65381113000120_070520261714.db    ← contagens (SQLite)
├── CONTAGEM_1_043_65381113000120_070520261714.pdf   ← relatório (conferência humana)
├── CONTAGEM_1_043_65381113000120_070520261714.sig   ← assinatura (JSON)
└── fotos/                                            ← fotos de produtos (opcional)
```

O `.db` é o arquivo que o ERP deve importar. **Antes de importar, o ERP deve
verificar a assinatura do `.db` contra o `.sig` e rejeitar a importação se a
verificação falhar** — isso impede importar um `.db` editado manualmente
(por exemplo, alguém abrindo o arquivo num editor SQLite e alterando uma
quantidade contada).

A verificação é **offline**: não depende de rede, de token de licença, nem de
consultar a CSCollectAPI. Só depende da chave pública fornecida abaixo.

---

## 2. Schema do `.db`

O `.db` é um arquivo SQLite com 3 tabelas, cada uma com uma coluna `tipo` fixa
(mesma convenção usada na carga que o Manager envia ao app):

```sql
CREATE TABLE Empresa (tipo TEXT, codempresa INTEGER, local TEXT, cnpj TEXT);
-- 1 linha, tipo = 'E'

CREATE TABLE Vendedor (tipo TEXT, codusuario INTEGER);
-- 1 linha, tipo = 'V'

CREATE TABLE Produtos (
    tipo TEXT, codean TEXT, codproduto TEXT, descricaoproduto TEXT, unidade TEXT,
    qtdecontada REAL, controlalote INTEGER, numlote TEXT, datafab TEXT, dataval TEXT,
    codgrupo TEXT, nomegrupo TEXT, localizacao TEXT
);
-- tipo = 'P', 1 linha por produto sem lote (controlalote=0, numlote/datafab/dataval = NULL)
-- + 1 linha por lote contado de produto com controle de lote (controlalote=1)
```

Um mesmo `codean` pode aparecer em várias linhas de `Produtos` quando
`controlalote=1` — uma linha por lote contado.

---

## 3. Formato do `.sig`

O `.sig` é um JSON (mesmo arquivo descrito em
[`VALIDACAO_ASSINATURA_SIG.md`](./VALIDACAO_ASSINATURA_SIG.md), que cobre a
validação feita pela CSCollectAPI). Os dois campos relevantes para o ERP são:

```json
{
  "payload": {
    "hash_db": "<sha256 hex do arquivo .db>"
  },
  "assinatura_rsa": "<base64 — assinatura RSA-2048/PKCS1v15-SHA256 do .db>"
}
```

- `payload.hash_db`: SHA-256 (hex) dos bytes crus do `.db`, calculado no
  momento da exportação.
- `assinatura_rsa`: assinatura RSA (PKCS#1 v1.5, hash SHA-256) desses mesmos
  bytes, feita com a chave **privada** — que só existe dentro do app LogScan.
  **Esta é a assinatura que o ERP deve verificar.**

O ERP não precisa (nem deve) tentar validar o campo `assinatura` (HMAC) —
esse é usado apenas entre o app e a CSCollectAPI.

---

## 4. Chave pública

RSA-2048, formato PEM (arquivo `export_db_public_key.pem`, neste mesmo
diretório):

```
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzWvISBG0DElIbeXn5GoB
NXGBz0NHBde4eLm87Rf3rtc82cR8P7j/P4lVce2FGTL2HmL3ZV8WZ7YdXW3CZ9YF
5mwMZnfgZxZNbcfYM7rGyoBh8ejyGhTJU3xvBuqSY0zM2DLXBYeL81isefGIQUKw
u/JKmTtJlntjzuyyU0iPyGwuC9Txz6w688Z3xAWoYMyhHMxz2PoOco937D7BEkDO
2yuq7aRvNXB4fT1s17kfSEhftOQl5LtSrDesyZMhAmSAbjWARhD+afumzFxPoHGI
GcD2U7DIQi3Kkkly4BPwYW+7C5quNkqottp7Fxvln2rd4+5240U2vHtg7HqMOFeu
PQIDAQAB
-----END PUBLIC KEY-----
```

Esta chave é **fixa para todo o app** (não muda por cliente/dispositivo) —
embuta-a uma única vez no ERP. Só a chave pública sai do LogScan; a chave
privada correspondente nunca é distribuída, então não é possível forjar um
`.db` válido sem ela.

---

## 5. Passo a passo de verificação

```
1. Extrair <nome>.db e <nome>.sig do ZIP
2. Ler <nome>.sig como JSON
3. Calcular SHA-256 dos bytes crus de <nome>.db
4. (opcional, sanity check) Conferir que esse SHA-256 == payload.hash_db
5. Decodificar assinatura_rsa de Base64 → bytes da assinatura RSA
6. Verificar a assinatura RSA (PKCS#1 v1.5, hash SHA-256) contra o SHA-256
   calculado no passo 3, usando a chave pública da seção 4
7. Se a verificação passar → .db íntegro, prosseguir com a importação
   Se falhar (passo 4 ou 6) → REJEITAR a importação, avisar o operador que
   o arquivo pode ter sido alterado manualmente
```

O passo crítico é o 6 (verificação RSA). O passo 4 é redundante com ele (se o
`.db` mudou, o hash muda, e a assinatura RSA sobre o hash antigo não bate mais
de qualquer forma) — mas é barato de checar e dá uma mensagem de erro mais
específica antes de gastar o custo de uma verificação RSA.

### Por que não basta comparar o hash (passo 4) sozinho?

Porque qualquer um pode recalcular um SHA-256. Um `.sig` forjado poderia trazer
o hash do `.db` já alterado. É a assinatura **RSA** (passo 6) que prova que o
hash foi produzido por quem tem a chave privada — ou seja, pelo app LogScan —
e não por quem alterou o arquivo depois.

---

## 6. Implementação em VB6 — nota prática

VB6 não tem uma API de RSA/SHA-256 utilizável diretamente. As opções são:

1. **CryptoAPI legado do Windows** (`advapi32.dll`: `CryptAcquireContext`,
   `CryptCreateHash`, `CryptHashData`, `CryptImportKey`, `CryptVerifySignature`
   com `CALG_RSA_SIGN`/`CALG_SHA_256`) — é o que verifica PKCS#1 v1.5
   nativamente (por isso esse foi o algoritmo escolhido, em vez de PSS, que
   exigiria CNG). Porém `CryptImportKey` espera a chave em formato de BLOB do
   Windows (`PUBLICKEYBLOB`), não em PEM/DER puro — é necessário converter a
   chave pública da seção 4 para esse formato uma única vez (offline, na hora
   de integrar) e embutir o BLOB resultante no ERP.
2. **Componente auxiliar** (recomendado): um pequeno DLL COM-visible (C#/.NET
   com `ComVisible(true)`, ou C++), expondo algo como:
   ```
   Function VerificarAssinaturaDb(caminhoDb As String, caminhoSig As String) As Boolean
   ```
   Internamente esse componente faz os passos 2–6 usando uma biblioteca de
   criptografia moderna (`System.Security.Cryptography.RSA` no .NET, por
   exemplo) e devolve `True`/`False` para o VB6 chamar via `CreateObject`.
   Isso evita lidar com BLOBs do CryptoAPI diretamente em VB6.

A escolha entre as duas fica a critério do programador do ERP — este
documento não assume qual delas será usada. O contrato que importa é: dado
`<nome>.db` e `<nome>.sig`, a verificação deve retornar um booleano claro
antes de qualquer importação.

---

## 7. Casos de rejeição

| Situação | O que fazer |
|---|---|
| `.sig` ausente no ZIP | Rejeitar — ZIP incompleto ou de versão muito antiga do app |
| SHA-256 do `.db` != `payload.hash_db` | Rejeitar — arquivo corrompido ou substituído |
| Assinatura RSA não verifica | Rejeitar — `.db` foi alterado manualmente após a exportação |
| Tudo confere | Prosseguir com a importação normalmente |

Em nenhum caso o ERP deve importar um `.db` cuja assinatura RSA não passou —
esse é justamente o ponto desta assinatura: impedir a importação de uma
contagem editada à mão.
