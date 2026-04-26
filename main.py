import os
import re
import httpx
import tempfile
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Você é um analista técnico de ISP especializado em registrar ocorrências de atendimento FTTH para continuidade operacional.

Tarefa: analisar o histórico do chat e gerar um LOG DE OCORRÊNCIA em UM ÚNICO PARÁGRAFO, conciso e útil para o próximo atendente.

OBJETIVO:
Permitir que outro atendente entenda rapidamente o que ocorreu, quais evidências foram coletadas, qual diagnóstico foi assumido e qual ação já foi tomada, evitando retrabalho.

REGRAS GERAIS:
- Não narre o atendimento. Extraia apenas informação útil.
- Use linguagem técnica, direta e concisa.
- Não mencione nomes, horários, CPF, cumprimentos ou menus.
- Priorize o diagnóstico explicitamente indicado pelo suporte.
- Não invente causas sem evidência no chat.

ESTRUTURA (em fluxo natural, sem rótulos):
Sintoma + Evidências + Contexto relevante + Diagnóstico + Ação

REGRAS DE FILTRAGEM:
- Inclua pelo menos 2 evidências objetivas quando disponíveis.
- Se teste via cabo também falhar → não descartar rede interna automaticamente.
- Se múltiplos clientes afetados → priorizar rede externa.
- Se sinal <= -25 dBm → considerar degradação física.
- Se houver quedas PPPoE → indicar instabilidade de link.

REGRAS DE COMPRESSÃO:
- Máximo de 5 frases.
- Máximo de 90 palavras.
- Remova tudo que não impacta diagnóstico ou continuidade.
- Não explique evidências; apenas declare (ex: “sinal -25 dBm”).

REGRAS DE ESTILO (CRÍTICAS):
- NUNCA use rótulos como "Evidências:", "Diagnóstico:", "Ação:" ou similares.
- NUNCA use dois pontos para separar seções.
- Escreva como texto corrido.
- Evite linguagem especulativa quando já houver diagnóstico definido.

EXEMPLO IDEAL:

"Cliente relatou lentidão geral em múltiplos dispositivos. ONU online, sem quedas, sinal em -20 dBm e teste via cabo com velocidade normal. Aproximadamente 10 dispositivos conectados ao Wi-Fi. Diagnóstico indica congestionamento na rede Wi-Fi. Orientado reinício do roteador, uso de rede 5 GHz e possível substituição do equipamento caso persista."

Histórico:
{chat_log}

Log de ocorrência:"""

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
    for url in audio_urls:
        transcription = await transcribe_audio(url)
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
