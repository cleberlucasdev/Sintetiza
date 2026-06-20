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

REPORT_PROMPT = """Você é um escriba de atendimentos de suporte técnico. Sua única função é resumir o que foi dito no chat, sem inventar, diagnosticar, propor soluções ou presumir nada além do que está explicitamente registrado.

Regras:
- Nada antes do atendimento do Suporte iniciar interessa para a análise.
- Seja conciso. Máximo 3 frases, máximo 80 palavras. Só o essencial: problema, o que foi feito, desfecho.
- Use tom impessoal. Nunca "o agente fez" ou "o suporte fez" — sempre "foi feito", "foi identificado", "foi orientado", "foi realizado".
- A escrita deve ser técnica e formal: o padrão esperado de um relatório para uma empresa: não use gírias nem expressões informais.
- Exclua informações que não são úteis para contextualização do atendimento e do que foi feito. Exemplo de informação a ser excluída: cliente agradeceu ao final do atendimento.
- Ignore completamente o fluxo do bot: CPF, menus, transferências, instruções automáticas. Foque só no atendimento do suporte: problema real e o que foi resolvido.
- Resuma apenas o que foi dito.
- Não diagnostique, não proponha ações, não faça perguntas.
- Se o atendimento foi curto ou inconclusivo, o relatório também será curto.
- Não mencione nomes de atendentes.
- Não mencione protocolos, horários, nem dados pessoais.
- Escreva em um único parágrafo, em português.

Exemplos: 
1) Chat: cliente entrou em contato mas não respondeu após ser atendido.
Relatório: "Cliente entrou em contato, mas cessou as interações. Atendimento encerrado por ausência de resposta."

2) Chat: cliente relatou lentidão, suporte fez ajustes no roteador, cliente confirmou melhora.
Relatório: "Cliente relatou lentidão na conexão. Realizados ajustes no roteador e cliente confirmou normalização."

3) Chat: cliente sem acesso, suporte identificou ONU offline, agendou visita técnica.
Relatório: "Cliente relatou ausência de conexão. Identificado sinal ausente na ONU. Visita técnica agendada."

Histórico:
{chat_log}

Relatório:"""

class ReportRequest(BaseModel):
    chat_log: str


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
        "model": "openai/gpt-oss-120b",
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
    prompt = REPORT_PROMPT.format(chat_log=processed_log)
    report = await generate_with_groq(prompt)
    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
