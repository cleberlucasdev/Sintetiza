import os
import re
import httpx
import tempfile
import asyncio
import time
import itertools
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

# Pool de API keys da Groq, em round-robin. A primeira é obrigatória; a segunda e a
# terceira são opcionais — se GROQ_API_KEY_2/_3 não estiverem setadas no ambiente,
# roda só com as que existirem (comportamento idêntico a antes com 1 key).
_raw_keys = [
    os.environ.get("GROQ_API_KEY"),
    os.environ.get("GROQ_API_KEY_2"),
    os.environ.get("GROQ_API_KEY_3"),
]
GROQ_API_KEYS = [k for k in _raw_keys if k]
if not GROQ_API_KEYS:
    raise RuntimeError("Nenhuma GROQ_API_KEY configurada")

KEY_COOLDOWN_SECONDS = 60  # tempo que uma key fica de fora do round-robin após falha própria dela

# Timestamp (time.monotonic) até quando cada key fica "de molho". Ausente = disponível.
_key_cooldown_until: dict[str, float] = {}
_key_cycle = itertools.cycle(range(len(GROQ_API_KEYS)))
_key_cycle_lock = asyncio.Lock()


def _key_label(key: str) -> str:
    # só os últimos 4 caracteres, pra logar sem expor a key inteira
    return f"...{key[-4:]}"


def _is_key_disabled(key: str) -> bool:
    return _key_cooldown_until.get(key, 0.0) > time.monotonic()


def _flag_key_unavailable(key: str, reason: str) -> None:
    _key_cooldown_until[key] = time.monotonic() + KEY_COOLDOWN_SECONDS
    print(f"[groq-keys] key {_key_label(key)} marcada indisponível por {KEY_COOLDOWN_SECONDS}s ({reason})")


async def _next_key() -> str:
    """Próxima key disponível em round-robin, pulando as em cooldown.
    Se todas estiverem em cooldown (situação extrema — pool inteiro sem cota),
    devolve a próxima da fila mesmo assim: melhor tentar com uma key suspeita
    do que recusar o request de cara."""
    async with _key_cycle_lock:
        for _ in range(len(GROQ_API_KEYS)):
            key = GROQ_API_KEYS[next(_key_cycle)]
            if not _is_key_disabled(key):
                return key
        return GROQ_API_KEYS[next(_key_cycle)]

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Resuma o atendimento de suporte técnico abaixo em um relatório técnico. Não invente, não diagnostique, não proponha ações, não faça perguntas — só relate o que consta no registro.

Regras:
- Considere só o atendimento em si (ignore mensagens automáticas/de bot: CPF, menus, transferências) e só o essencial: problema, o que foi feito, desfecho. Corte o irrelevante (ex: agradecimentos).
- Não inclua movimentação interna do atendimento: troca de atendente, atendente se ausentando/passando o turno, transferências entre agentes. Isso é logística, não sintoma.
- Não inclua orientações genéricas de primeiro nível dadas automaticamente (ex: "desligue o aparelho da energia por 30 segundos") a menos que tenham sido a ação que resolveu o problema. O que importa é o sintoma relatado e o que foi tecnicamente feito, não o roteiro padrão de atendimento.
- Tom impessoal e formal, sem gírias: "foi feito/identificado/orientado", nunca "o agente/suporte fez".
- Um único parágrafo, em português. Sem nomes de atendentes, protocolos, horários ou dados pessoais.

HIERARQUIA DE FATOS (aplique antes de escrever qualquer frase):
Todo fato do atendimento se encaixa em uma destas categorias. Só as 4 primeiras geram frase própria:
1. Sintoma relatado pelo cliente — sempre incluir.
2. Ação ou verificação que MUDOU o resultado (resolveu, alterou o diagnóstico, ou foi a causa raiz identificada) — sempre incluir.
3. Conclusão/diagnóstico final — sempre incluir, uma única vez.
4. Encaminhamento ou desfecho dado ao cliente — sempre incluir.
5. Testes, checagens ou verificações que NÃO alteraram o resultado (descartaram uma hipótese, confirmaram que algo já estava correto, ou não mudaram o quadro) — NUNCA listar individualmente. Se houver uma ou mais dessas, funda todas em uma única frase genérica indicando que verificações foram feitas sem alteração no quadro (ex: "Foram verificadas configurações do dispositivo e da conexão, sem alteração no comportamento identificado."). Não cite quais especificamente, não cite valores técnicos (taxas, dBm, versões) a menos que esse valor específico tenha sido a causa raiz confirmada.
6. Reafirmação da mesma conclusão sob ângulos diferentes (ex: dizer "não é a rede" de três formas ao longo do atendimento) — mencione uma vez só, na frase de conclusão.

Regra de ouro: uma ação só merece frase própria se ela (a) resolveu o problema, (b) mudou o diagnóstico, ou (c) é o encaminhamento final. Tudo que é etapa de descarte vira UMA menção coletiva, nunca uma lista.

- Não existe número fixo de frases — o tamanho é ditado pela quantidade de fatos das categorias 1-4, nunca pela quantidade de linhas do chat. Um atendimento com 1 sintoma e 1 ação cabe em 1 frase. Um atendimento com várias categorias 1-4 distintas pode legitimamente precisar de mais frases — mas cada frase carrega um fato novo dessas categorias, nunca reforça, explica ou reformula um fato já dito.
- Proibido: frases de transição ("Diante disso", "Após isso"), floreio, redundância semântica, explicação de contexto óbvio.
- Atendimento curto/inconclusivo → relatório curto.
- Se houver "Informações adicionais": elas descrevem o que foi tecnicamente executado de fato, do ponto de vista interno do suporte, e têm PRECEDÊNCIA sobre o chat (ex: chat diz "atualizei o roteador", adicional diz "troquei DNS e bloco de IP" → relatório reflete o adicional). Nunca trate esse campo como fala do cliente ou do chat.
- PROIBIDO citar a origem da informação no texto (ex: "informações adicionais indicam que", "de acordo com o campo adicional", "conforme relatado internamente"). O relatório narra os fatos como fatos, ponto — nunca menciona de qual campo de entrada (chat ou adicional) cada fato veio. Errado: "Informações adicionais indicam que foi realizada manutenção no backbone." Certo: "Foi realizada manutenção no backbone."

Exemplos:
1) Chat: cliente relatou lentidão, suporte fez ajustes no roteador, cliente confirmou melhora. Sem informações adicionais.
Relatório: "Cliente relatou lentidão na conexão. Realizados ajustes no roteador e cliente confirmou normalização."

2) Chat: cliente relatou lentidão, suporte disse que "atualizou o roteador". Informações adicionais: "troquei o DNS para 8.8.8.8 e mudei o bloco de IP do cliente para o pool novo".
Relatório: "Cliente relatou lentidão na conexão. Foram realizados alteração do servidor DNS e migração do endereço IP do cliente para novo bloco. Cliente confirmou normalização."

3) Chat: cliente relatou internet ruim em todos os aplicativos e no computador; bot orientou desligar o aparelho por 30 segundos; atendimento passou por dois atendentes diferentes (um encerrou o turno); o segundo perguntou se normalizou e o cliente não respondeu; atendimento encerrado por falta de retorno.
Relatório: "Cliente relatou lentidão de conexão afetando múltiplos aplicativos e dispositivos. Não houve retorno do cliente para confirmar normalização, e o atendimento foi encerrado por ausência de resposta."

4) Chat: cliente relatou instabilidade intermitente há 3 dias, quedas de sinal em horários aleatórios; suporte verificou sinal óptico da ONU e encontrou nível baixo (-28dBm); trocou o cabo drop; suporte também identificou firmware desatualizado no roteador e atualizou; cliente testou e confirmou estabilidade.
Relatório: "Cliente relatou instabilidade intermitente com quedas de sinal há 3 dias. Identificado nível de sinal óptico baixo na ONU (-28dBm) e realizada troca do cabo drop. Firmware do roteador estava desatualizado e foi atualizado. Cliente confirmou estabilidade após as correções."

5) Chat: cliente relatou velocidade limitada a ~100Mbps apenas no MacBook (cabo e Wi-Fi), enquanto outros dispositivos ficavam normais; houve visita técnica com ajuste de configurações e atualização do roteador; suporte verificou configuração da placa de rede Ethernet do Mac (estava automática, testou manual em 1000Mbps, sem mudança), verificou Network Link Conditioner (desligado), verificou modo de baixa carga de dados (desligado), confirmou taxa de enlace Wi-Fi de 1201Mbps e testou novamente via cabo — nenhuma dessas checagens alterou o teto de 100Mbps; concluiu-se que a limitação está no próprio equipamento; cliente foi orientado a buscar assistência técnica especializada no Mac. Oscilações anteriores (quedas a 20-30Mbps) cessaram após a visita.
Relatório: "Cliente relatou velocidade limitada a aproximadamente 100Mbps no MacBook, tanto via cabo quanto via Wi-Fi, enquanto os demais dispositivos operavam normalmente. Foram verificadas configurações de rede e hardware do equipamento, sem alteração no comportamento identificado. Concluiu-se que a limitação está associada ao próprio equipamento, e o cliente foi orientado a buscar assistência técnica especializada para avaliação do aparelho. Oscilações de conexão relatadas em dias anteriores cessaram após visita técnica realizada."

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

        try:
            for _ in range(len(GROQ_API_KEYS)):
                key = await _next_key()
                with open(tmp_path, "rb") as audio_file:
                    response = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": ("audio.ogg", audio_file, "audio/ogg")},
                        data={"model": "whisper-large-v3", "language": "pt"},
                    )

                if response.status_code in (401, 429):
                    _flag_key_unavailable(key, f"HTTP {response.status_code}")
                    continue

                if response.status_code != 200:
                    return "[transcrição indisponível]"

                return response.json().get("text", "[transcrição indisponível]")

            return "[transcrição indisponível]"
        finally:
            os.unlink(tmp_path)


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
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": GROQ_MAX_COMPLETION_TOKENS,
    }
    last_key_error = None
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(len(GROQ_API_KEYS)):
            key = await _next_key()
            try:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                detail = e.response.text[:300]
                if status in (401, 429):
                    # Falha da própria key (inválida/revogada ou cota estourada) —
                    # marca essa key de fora e tenta a próxima do pool.
                    _flag_key_unavailable(key, f"HTTP {status}")
                    last_key_error = f"Falha ao gerar relatório na Groq ({status}): {detail}"
                    continue
                # Erro que não é sobre a key (ex: prompt malformado, 5xx da Groq) —
                # trocar de key não resolve nada, então propaga direto.
                raise HTTPException(
                    status_code=502,
                    detail=f"Falha ao gerar relatório na Groq ({status}): {detail}",
                )
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Falha de conexão com a Groq: {e}")

            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"].strip()

            if choice.get("finish_reason") == "length":
                print(f"[generate-report] resposta truncada por max_tokens ({GROQ_MAX_COMPLETION_TOKENS})")

            return {
                "content": content,
                "usage": data.get("usage", {}),
            }

    # todas as keys do pool falharam com 401/429
    raise HTTPException(
        status_code=502,
        detail=last_key_error or "Todas as keys da Groq estão indisponíveis no momento.",
    )


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
