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

ESTRUTURA OBRIGATÓRIA (dentro do parágrafo):
[Sintoma] + [Evidências] + [Contexto relevante] + [Diagnóstico] + [Ação]

DEFINIÇÕES:
- Sintoma: problema relatado pelo cliente (ex: instabilidade, sem sinal).
- Evidências: dados objetivos (ex: teste via cabo, quedas PPPoE, nível de sinal).
- Contexto relevante: apenas o que ajuda próximos atendimentos (ex: chuva, recorrência, múltiplos clientes afetados).
- Diagnóstico: causa mais provável definida pelo suporte.
- Ação: o que foi feito (ex: ajuste remoto, agendamento, abertura de chamado).

REGRAS DE FILTRAGEM:
- Inclua OBRIGATORIAMENTE pelo menos 2 evidências objetivas quando disponíveis.
- Se teste via cabo também falhar → não descartar rede interna automaticamente.
- Se múltiplos clientes afetados → priorizar rede externa.
- Se sinal <= -25 dBm → considerar degradação física.
- Se houver quedas PPPoE → indicar instabilidade de link.

REGRAS DE COMPRESSÃO:
- Máximo de 5 frases.
- Máximo de 90 palavras.
- Inclua apenas informações que impactam diagnóstico ou continuidade.
- Remova:
  - cumprimentos
  - repetições
  - perguntas intermediárias
  - detalhes irrelevantes (ex: cores de LED sem impacto)
- Não explique evidências; apenas declare (ex: “sinal -25 dBm”).

REGRA DE QUALIDADE (CRÍTICA):
- O texto deve permitir que o próximo atendente continue o atendimento sem repetir diagnóstico básico.

EXEMPLO IDEAL:

"Cliente relatou instabilidade com quedas constantes ao longo do dia. Verificado ONU online com quedas PPPoE e sinal em -25 dBm. Teste via cabo também apresentou falha. Cliente informou ocorrência após chuva forte. Identificada degradação de sinal óptico. Foi aberto um chamado para equipe externa verificar cabo drop e conectores, com previsão para o dia seguinte."

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
