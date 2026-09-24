# Instagram Auto Publisher

Programa desktop (Windows) para automatizar a publicação de vários vídeos no Instagram usando **Python + Playwright**, com navegador visível e sessão persistente.

Você escolhe a pasta, escreve **uma única descrição** (usada em todos os vídeos) e clica em **INICIAR**. O programa publica os vídeos um por um, aguarda a confirmação real de cada publicação e registra o progresso.

> **Aviso:** automação no site do Instagram viola os Termos de Uso da plataforma e pode gerar restrições na sua conta. Use com moderação, com intervalos entre publicações, e por sua própria conta e risco. O programa **não burla** CAPTCHA, 2FA ou qualquer verificação de segurança — quando o Instagram pedir ação manual, ele pausa e espera você resolver.

---

## Requisitos

- Windows 10/11
- Python 3.11 ou superior
- Google Chrome instalado (recomendado). Sem Chrome, o programa usa Microsoft Edge; sem ambos, instale o Chromium com `playwright install chromium`.

## Instalação

```bash
cd instagram_auto_publisher
pip install -r requirements.txt
playwright install
```

O comando `playwright install` baixa o Chromium usado como fallback (opcional se você já tem Chrome).

## Primeiro login

1. Execute o programa:

   ```bash
   python main.py
   ```

2. Clique em **INICIAR PUBLICACAO** (mesmo sem pasta — ou selecione uma pasta antes).
3. O navegador abrirá o Instagram. Se você não estiver logado, o painel mostrará:

   ```text
   ⚠ ACAO MANUAL NECESSARIA - Faça login no Instagram na janela do navegador.
   ```

4. Faça login normalmente (inclusive 2FA/captcha, se pedido). O programa detecta o login sozinho e continua.
5. A sessão fica salva na pasta `browser_profile/`. Nas próximas execuções o login é reutilizado. **Nada de senha é armazenado pelo programa.**

## Uso diário

1. Edite seus vídeos e coloque-os em uma pasta (`mp4`, `mov`, `webm`, `mkv`).
2. Abra o programa → **Selecionar pasta** → confira a lista/quantidade de vídeos.
3. Escreva a **descrição padrão** (com quebras de linha e hashtags exatamente como quer).
4. Clique em **INICIAR PUBLICACAO**.
5. Deixe trabalhando: para cada vídeo o programa faz upload → espera processamento → insere a descrição → publica → confirma → passa ao próximo.

A ordem dos vídeos é numérica quando os nomes têm números (`video_01`, `video_02`, …), senão alfabética. Se existirem dois arquivos com o mesmo nome base (ex.: `clip.mp4` e `clip.mov`), o `.mp4` tem prioridade.

## Pausar / Continuar / Cancelar

- **PAUSAR**: não inicia novos vídeos; termina a etapa atual com segurança. O botão vira **CONTINUAR**.
- **CANCELAR**: interrompe de forma segura após a etapa atual, salva o estado e fecha o navegador.

## Retomar de onde parou

Toda sessão é gravada em `data/state.json` (pasta, descrição da época e status de cada vídeo).

- Ao reabrir o programa, se houver sessão pendente aparece um aviso com botões **Retomar** (continua pulando os já publicados) e **Descartar** (apaga o histórico).
- Vídeos que falharam ficam marcados `✗ Erro`; use **Tentar novamente falhas** (roda novamente os não publicados) ou **Ignorar falhas**.

Para começar tudo do zero: Descartar a sessão no aviso inicial, ou apagar `data/state.json`.

## Trocar de conta / limpar a sessão do navegador

Feche o programa e apague a pasta `browser_profile/`. Na próxima execução ele pedirá login novamente.

## Configuração

Arquivo `data/config.json` (criado automaticamente; pode ser editado com o programa fechado):

| Chave | Padrão | Descrição |
|---|---|---|
| `default_folder` | `""` | Última pasta usada |
| `last_description` | `""` | Última descrição digitada |
| `require_description` | `true` | Exige descrição (desligável também pela checkbox da tela) |
| `max_attempts` | `2` | Tentativas por vídeo antes de marcar erro |
| `headless` | `false` | Mantenha `false`: navegador visível |
| `browser_channel` | `"auto"` | `auto`, `chrome`, `msedge` ou `chromium` |
| `viewport_width/height` | `1280x860` | Tamanho da janela |
| `locale` | `"pt-BR"` | Idioma preferido do navegador |
| `action_delay_min_ms/max_ms` | `300–900` | Pequenas pausas aleatórias entre ações |
| `delay_between_videos_s` | `5.0` | Intervalo entre um vídeo e outro |
| `upload_timeout_s` | `900` | Tempo máximo de upload/processamento por vídeo |
| `step_timeout_s` | `120` | Espera máxima por telas intermediárias |
| `publish_confirm_timeout_s` | `180` | Espera máxima pela confirmação da publicação |
| `login_wait_timeout_s` | `1200` | Quanto tempo espera você logar manualmente |
| `stop_on_error` | `false` | Parar tudo no primeiro vídeo com erro |
| `log_level` | `"INFO"` | `INFO` ou `DEBUG` |

Não há — e nunca haverá — senha salva em nenhum arquivo deste projeto.

## Logs e erros

- Log completo em `logs/publisher.log` (e também no painel da interface).
- Em falhas importantes é salvo screenshot em `logs/errors/<video>_<etapa>_<hora>.png` — essencial para diagnosticar mudanças na interface do Instagram.

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| "O diálogo de criação de publicação não apareceu" | O Instagram provavelmente mudou o layout. Veja o screenshot em `logs/errors/` e ajuste os seletores em `instagram/selectors.py`. |
| Fica preso no login | Confirme que concluiu o login na janela aberta; se trocou de conta, apague `browser_profile/`. |
| Erro ao selecionar arquivo | Verifique se o vídeo abre fora do programa e se está em `.mp4/.mov/.mkv/.webm`. Nomes muito longos/acento podem atrapalhar — teste renomeando. |
| Timeout no processamento | Vídeo grande/pesado: aumente `upload_timeout_s` em `config.json`. |
| Chrome não encontrado | Rode `playwright install chromium` ou defina `browser_channel`. |

Os textos dos botões são procurados em vários idiomas (`Next/Avançar`, `Share/Publicar/Compartilhar`, etc.). Para outro idioma, basta acrescentar o texto nas listas de `instagram/selectors.py`.

## Estrutura do projeto

```text
instagram_auto_publisher/
├── main.py                  # entrada do programa
├── config.py                # configuração + caminhos
├── requirements.txt
├── README.md
├── gui/
│   └── app.py               # interface CustomTkinter + thread worker
├── instagram/
│   ├── browser.py           # Playwright + perfil persistente
│   ├── login.py             # verificação/login manual assistido
│   ├── publisher.py         # ciclo completo de publicação
│   ├── description.py       # preenchimento/verificação da legenda
│   └── selectors.py         # TODOS os seletores centralizados
├── core/
│   ├── video_manager.py     # descoberta e ordenação dos vídeos
│   ├── state_manager.py     # estado persistente da sessão
│   └── logger.py            # logs arquivo + GUI
├── data/                    # config.json e state.json
├── browser_profile/         # perfil/sessão do navegador (não versionar)
└── logs/                    # logs e screenshots de erro
```

## Dica de adoção gradual

Antes de usar no dia a dia, teste assim:

1. 2 vídeos → 2. 5 vídeos → 3. 10 vídeos → 4. 30 vídeos.

Se algo falhar consistentemente numa etapa, ajuste o seletor correspondente em `instagram/selectors.py` usando o screenshot salvo como referência.
