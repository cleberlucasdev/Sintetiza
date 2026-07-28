import os
import re
import httpx
import tempfile
import asyncio
import asyncpg
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.head("/health")
async def health_head():
    return Response(status_code=200)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DATABASE_URL = os.environ.get("DATABASE_URL")  # string de conexão do Postgres (ex: Neon), fica em outro servidor

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS usage_log (
    id SERIAL PRIMARY KEY,
    attendant TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    prompt_tokens INT,
    completion_tokens INT,
    total_tokens INT
);
"""


@app.on_event("startup")
async def ensure_table_exists():
    # Cria a tabela na primeira vez que o servidor sobe, se ainda não existir.
    # Não trava o startup do app se o banco estiver fora do ar.
    if not DATABASE_URL:
        print("[usage_log] DATABASE_URL não configurada — tracking de uso desabilitado.")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(CREATE_TABLE_SQL)
        await conn.close()
    except Exception as e:
        print(f"[usage_log] falha ao garantir tabela: {e}")


async def log_usage(attendant: str, usage: dict):
    if not DATABASE_URL:
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(
            "INSERT INTO usage_log (attendant, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES ($1, $2, $3, $4)",
            attendant or "desconhecido",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
        await conn.close()
    except Exception as e:
        # Métrica é secundária: se o banco falhar, o relatório ainda deve ser entregue.
        print(f"[usage_log] falha ao gravar uso: {e}")


AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Resuma o atendimento de suporte técnico abaixo em um relatório técnico. Não invente, não diagnostique, não proponha ações, não faça perguntas — só relate o que consta no registro.

Regras:
- Considere só o atendimento em si (ignore mensagens automáticas/de bot: CPF, menus, transferências) e só o essencial: problema, o que foi feito, desfecho. Corte o irrelevante (ex: agradecimentos).
- Não inclua movimentação interna do atendimento: troca de atendente, atendente se ausentando/passando o turno, transferências entre agentes. Isso é logística, não sintoma.
- Não inclua orientações genéricas de primeiro nível dadas automaticamente (ex: "desligue o aparelho da energia por 30 segundos") a menos que tenham sido a ação que resolveu o problema. O que importa é o sintoma relatado e o que foi tecnicamente feito, não o roteiro padrão de atendimento.
- Tom impessoal e formal, sem gírias: "foi feito/identificado/orientado", nunca "o agente/suporte fez".
- Um único parágrafo, em português. Sem nomes de atendentes, protocolos, horários ou dados pessoais.
- Não existe número fixo de frases — o tamanho é ditado pela quantidade de fatos reais, nunca por vontade de elaborar. Regra: máxima densidade. Um atendimento com 1 sintoma e 1 ação cabe em 1 frase. Um atendimento com vários sintomas ou várias ações distintas pode legitimamente precisar de mais frases — mas cada frase carrega um fato novo, nunca reforça, explica ou reformula um fato já dito. Se uma frase pode ser cortada sem perder informação, ela deve ser cortada.
- Proibido: frases de transição ("Diante disso", "Após isso"), floreio, redundância semântica (dizer a mesma coisa 2x com sinônimos), explicação de contexto óbvio (ex: não precisa dizer que reiniciar resolve problemas comuns — só diga que foi feito e o resultado).
- Atendimento curto/inconclusivo → relatório curto.
- Se houver "Informações adicionais": elas descrevem o que foi tecnicamente executado de fato, do ponto de vista interno do suporte, e têm PRECEDÊNCIA sobre o chat (ex: chat diz "atualizei o roteador", adicional diz "troquei DNS e bloco de IP" → relatório reflete o adicional). Nunca trate esse campo como fala do cliente ou do chat.

Exemplos:
1) Chat: cliente relatou lentidão, suporte fez ajustes no roteador, cliente confirmou melhora. Sem informações adicionais.
Relatório: "Cliente relatou lentidão na conexão. Realizados ajustes no roteador e cliente confirmou normalização."

2) Chat: cliente relatou lentidão, suporte disse que "atualizou o roteador". Informações adicionais: "troquei o DNS para 8.8.8.8 e mudei o bloco de IP do cliente para o pool novo".
Relatório: "Cliente relatou lentidão na conexão. Foram realizados alteração do servidor DNS e migração do endereço IP do cliente para novo bloco. Cliente confirmou normalização."

3) Chat: cliente relatou internet ruim em todos os aplicativos e no computador; bot orientou desligar o aparelho por 30 segundos; atendimento passou por dois atendentes diferentes (um encerrou o turno); o segundo perguntou se normalizou e o cliente não respondeu; atendimento encerrado por falta de retorno.
Relatório: "Cliente relatou lentidão de conexão afetando múltiplos aplicativos e dispositivos. Não houve retorno do cliente para confirmar normalização, e o atendimento foi encerrado por ausência de resposta."

4) Chat: cliente relatou instabilidade intermitente há 3 dias, quedas de sinal em horários aleatórios; suporte verificou sinal óptico da ONU e encontrou nível baixo (-28dBm); trocou o cabo drop; suporte também identificou firmware desatualizado no roteador e atualizou; cliente testou e confirmou estabilidade.
Relatório: "Cliente relatou instabilidade intermitente com quedas de sinal há 3 dias. Identificado nível de sinal óptico baixo na ONU (-28dBm) e realizada troca do cabo drop. Firmware do roteador estava desatualizado e foi atualizado. Cliente confirmou estabilidade após as correções."

Histórico do chat:
{chat_log}

Informações adicionais (interno, pode não ter sido dito ao cliente):
{additional_info}

Relatório:"""

class ReportRequest(BaseModel):
    chat_log: str
    additional_info: str = ""
    attendant: str = ""


async def transcribe_audio(url: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        audio_response = await client.get(url)
        if audio_response.status_code != 200:
            return "[transcrição indisponível]"

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_response.content)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.ogg", audio_file, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "pt"},
            )

        os.unlink(tmp_path)

        if response.status_code != 200:
            return "[transcrição indisponível]"

        return response.json().get("text", "[transcrição indisponível]")


async def process_chat_log(chat_log: str) -> str:
    audio_urls = AUDIO_PATTERN.findall(chat_log)
    if not audio_urls:
        return chat_log
    
    transcriptions = await asyncio.gather(*[transcribe_audio(url) for url in audio_urls])
    
    for transcription in transcriptions:
        chat_log = AUDIO_PATTERN.sub(
            f"[ÁUDIO TRANSCRITO: {transcription}]",
            chat_log,
            count=1
        )
    return chat_log


async def generate_with_groq(prompt: str) -> dict:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 350,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return {
            "content": data["choices"][0]["message"]["content"].strip(),
            "usage": data.get("usage", {}),
        }


@app.post("/generate-report")
async def generate_report(request: ReportRequest, background_tasks: BackgroundTasks):
    if not request.chat_log.strip():
        raise HTTPException(status_code=400, detail="chat_log vazio")

    processed_log = await process_chat_log(request.chat_log)
    additional_info = request.additional_info.strip() or "(nenhuma)"
    prompt = REPORT_PROMPT.format(
        chat_log=processed_log,
        additional_info=additional_info,
    )
    result = await generate_with_groq(prompt)

    # Gravar o uso não pode atrasar a resposta pro atendente — roda depois de responder.
    background_tasks.add_task(log_usage, request.attendant, result["usage"])

    return {"report": result["content"]}


@app.get("/health")
async def health():
    return {"status": "ok"}
