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


GROQ_MAX_COMPLETION_TOKENS = 2000  # teto de saída com folga grande de propósito — quem garante o tamanho enxuto é o prompt, não esse limite

# Limite defensivo de entrada: chat_log + additional_info muito grandes podem estourar o
# TPM (tokens por minuto) do plano da Groq ou o contexto aceito pelo modelo no request,
# fazendo a API devolver erro em vez de 200. Cortamos ANTES de montar o prompt, mantendo
# o fim do texto (onde geralmente está o desfecho/confirmação do cliente).
MAX_INPUT_CHARS = 12000


def _truncate_input(text: str, label: str) -> str:
    if len(text) <= MAX_INPUT_CHARS:
        return text
    return f"[...trecho inicial omitido por tamanho...]\n{text[-MAX_INPUT_CHARS:]}"


async def generate_with_groq(prompt: str) -> dict:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": GROQ_MAX_COMPLETION_TOKENS,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Erro real da Groq (ex: contexto estourado, rate limit, chave inválida).
            # Antes isso subia cru e virava 500 sem explicação nenhuma pro atendente.
            detail = e.response.text[:300]
            raise HTTPException(
                status_code=502,
                detail=f"Falha ao gerar relatório na Groq ({e.response.status_code}): {detail}",
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Falha de conexão com a Groq: {e}")

        data = response.json()
        choice = data["choices"][0]
        content = choice["message"]["content"].strip()

        # Se o modelo foi cortado pelo teto de tokens, o relatório sai pela metade
        # (finish_reason == "length") mesmo sem nenhum erro HTTP. Sinaliza no log
        # do Render pra dar pra rastrear caso volte a acontecer com o novo teto.
        if choice.get("finish_reason") == "length":
            print(f"[generate-report] resposta truncada por max_tokens ({GROQ_MAX_COMPLETION_TOKENS})")

        return {
            "content": content,
            "usage": data.get("usage", {}),
        }


@app.post("/generate-report")
async def generate_report(request: ReportRequest):
    if not request.chat_log.strip():
        raise HTTPException(status_code=400, detail="chat_log vazio")

    processed_log = await process_chat_log(request.chat_log)
    processed_log = _truncate_input(processed_log, "chat_log")
    additional_info = _truncate_input(request.additional_info.strip(), "additional_info") or "(nenhuma)"
    prompt = REPORT_PROMPT.format(
        chat_log=processed_log,
        additional_info=additional_info,
    )
    result = await generate_with_groq(prompt)

    return {"report": result["content"]}


@app.get("/health")
async def health():
    return {"status": "ok"}
