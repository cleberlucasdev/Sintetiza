import os
import re
import httpx
import tempfile
import asyncio
from fastapi import FastAPI, HTTPException
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

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Resuma o atendimento de suporte técnico abaixo em um relatório técnico. Não invente, não diagnostique, não proponha ações, não faça perguntas — só relate o que consta no registro.

Regras:
- Considere só o atendimento em si (ignore mensagens automáticas/de bot: CPF, menus, transferências) e só o essencial: problema, o que foi feito, desfecho. Corte o irrelevante (ex: agradecimentos).
- Não inclua movimentação interna do atendimento: troca de atendente, atendente se ausentando/passando o turno, transferências entre agentes. Isso é logística, não sintoma.
- Não inclua orientações genéricas de primeiro nível dadas automaticamente (ex: "desligue o aparelho da energia por 30 segundos") a menos que tenham sido a ação que resolveu o problema. O que importa é o sintoma relatado e o que foi tecnicamente feito, não o roteiro padrão de atendimento.
- Tom impessoal e formal, sem gírias: "foi feito/identificado/orientado", nunca "o agente/suporte fez".
- Um único parágrafo, em português. Sem nomes de atendentes, protocolos, horários ou dados pessoais.
- Atendimento curto/inconclusivo → relatório curto.
- Se houver "Informações adicionais": elas descrevem o que foi tecnicamente executado de fato, do ponto de vista interno do suporte, e têm PRECEDÊNCIA sobre o chat (ex: chat diz "atualizei o roteador", adicional diz "troquei DNS e bloco de IP" → relatório reflete o adicional). Nunca trate esse campo como fala do cliente ou do chat.

Exemplos:
1) Chat: cliente relatou lentidão, suporte fez ajustes no roteador, cliente confirmou melhora. Sem informações adicionais.
Relatório: "Cliente relatou lentidão na conexão. Realizados ajustes no roteador e cliente confirmou normalização."

2) Chat: cliente relatou internet ruim em todos os aplicativos e no computador; bot orientou desligar o aparelho por 30 segundos; atendimento passou por dois atendentes diferentes (um encerrou o turno); o segundo perguntou se normalizou e o cliente não respondeu; atendimento encerrado por falta de retorno.
Relatório: "Cliente relatou lentidão de conexão afetando múltiplos aplicativos e dispositivos. Não houve retorno do cliente para confirmar normalização, e o atendimento foi encerrado por ausência de resposta."

2) Chat: cliente relatou lentidão, suporte disse que "atualizou o roteador". Informações adicionais: "troquei o DNS para 8.8.8.8 e mudei o bloco de IP do cliente para o pool novo".
Relatório: "Cliente relatou lentidão na conexão. Foram realizados alteração do servidor DNS e migração do endereço IP do cliente para novo bloco. Cliente confirmou normalização."

Histórico do chat:
{chat_log}

Informações adicionais (interno, pode não ter sido dito ao cliente):
{additional_info}

Relatório:"""

class ReportRequest(BaseModel):
    chat_log: str
    additional_info: str = ""


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


async def generate_with_groq(prompt: str) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


@app.post("/generate-report")
async def generate_report(request: ReportRequest):
    if not request.chat_log.strip():
        raise HTTPException(status_code=400, detail="chat_log vazio")

    processed_log = await process_chat_log(request.chat_log)
    additional_info = request.additional_info.strip() or "(nenhuma)"
    prompt = REPORT_PROMPT.format(
        chat_log=processed_log,
        additional_info=additional_info,
    )
    report = await generate_with_groq(prompt)
    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
