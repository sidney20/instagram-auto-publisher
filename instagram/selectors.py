from __future__ import annotations

INSTAGRAM_HOME_URL = "https://www.instagram.com/"
CREATE_URL_FALLBACK = "https://www.instagram.com/create/style/"

DIALOG_CSS = 'div[role="dialog"]'

CREATE_BUTTON_LABELS = [
    "New post",
    "Nova publicação",
    "Nuevo post",
    "Nueva publicación",
    "Nouvelle publication",
    "Neuer Beitrag",
    "Novo post",
]
CREATE_CSS_CANDIDATES = ['a[href*="/create"]']

SELECT_FROM_COMPUTER_TEXTS = [
    "Select from computer",
    "Selecionar do computador",
    "Seleccionar del ordenador",
    "Sélectionner sur l'ordinateur",
    "Vom Computer auswählen",
]

FILE_INPUT_ACCEPT_VIDEO = 'input[type="file"][accept*="video"]'
FILE_INPUT_ANY = (
    'div[role="dialog"] input[type="file"], '
    'form input[type="file"], '
    'input[type="file"]'
)

NEXT_BUTTON_TEXTS = [
    "Next",
    "Avançar",
    "Avance",
    "Siguiente",
    "Suivant",
    "Weiter",
    "Próximo",
    "Proximo",
]

SHARE_BUTTON_TEXTS = [
    "Share",
    "Post",
    "Publicar",
    "Compartilhar",
    "Compartir",
    "Partager",
    "Teilen",
]

CAPTION_ARIA_LABELS = [
    "Write a caption...",
    "Write a caption…",
    "Escreva uma legenda...",
    "Escreva uma legenda…",
    "Escribe un pie de foto...",
    "Ajouter une légende...",
    "Bildunterschrift hinzufügen...",
    "Adicionar legenda...",
]
CAPTION_CSS_BY_LABEL = [
    'textarea[aria-label="{label}"]',
    'textarea[placeholder="{label}"]',
    'div[contenteditable="true"][aria-label="{label}"]',
]
CAPTION_GENERIC_CSS = [
    'div[role="dialog"] textarea',
    'div[role="dialog"] div[contenteditable="true"]',
    'form textarea',
]

SUCCESS_TEXTS = [
    "Your post has been shared",
    "Your video has been shared",
    "Your reel has been shared",
    "Post shared",
    "Reel shared",
    "Sua publicação foi compartilhada",
    "Seu vídeo foi compartilhado",
    "Seu reel foi compartilhado",
    "O seu reel foi compartilhado",
    "Tu publicación se ha compartido",
    "Se compartió tu publicación",
    "Votre publication a été partagée",
    "Dein Beitrag wurde geteilt",
]

ERROR_TOAST_TEXTS = [
    "Couldn't post",
    "Couldn't share",
    "Try again later",
    "Try Again",
    "Não foi possível publicar",
    "Não foi possível compartilhar",
    "Tente novamente mais tarde",
    "Tentar novamente",
    "No se pudo compartir",
    "Impossible de publier",
]

DISCARD_TEXTS = [
    "Discard",
    "Discard post",
    "Descartar",
    "Descartar publicação",
    "Descartar post",
    "Descartar",
]

LOGIN_INPUT_CSS = 'input[name="username"]'
LOGIN_PASSWORD_CSS = 'input[name="password"]'

CHALLENGE_URL_MARKERS = [
    "/challenge",
    "/accounts/login",
    "/accounts/access",
    "/ajax/challenge",
    "/two_factor",
]

SESSION_COOKIE_NAME = "sessionid"

EXPAND_BUTTON_ARIA = [
    "Expand",
    "Expandir",
    "Original",
    "Tamanho original",
    "Full size",
    "Expand to original size",
]
EXPAND_CSS_CANDIDATES = [
    'button[aria-label="{label}"]',
    'div[role="button"][aria-label="{label}"]',
    'svg[aria-label="{label}"]',
]
EXPAND_ICON_CSS = [
    'div[role="dialog"] button[aria-label*="expand" i]',
    'div[role="dialog"] button[aria-label*="Expandir" i]',
    'div[role="dialog"] svg[aria-label*="expand" i]',
]

CONCLUDE_BUTTON_TEXTS = [
    "Concluir",
    "Done",
    "Share",
    "Post",
    "Publicar",
    "Compartilhar",
    "Compartir",
    "OK",
    "OK",
]

CONFIRM_DIALOG_TEXTS = [
    "Your post has been shared",
    "Sua publicação foi compartilhada",
    "Seu vídeo foi compartilhado",
    "Post shared",
    "Tu publicación se ha compartido",
    "Votre publication a été partagée",
    "Dein Beitrag wurde geteilt",
]

COVER_STRIP_ARIA = [
    "Cover",
    "Capa",
    "Select cover",
    "Selecionar capa",
    "Seleccionar portada",
    "Choisir la couverture",
]

COVER_SLIDER_ROLE_TEXTS = COVER_STRIP_ARIA

COVER_RANGE_INPUT_CSS = [
    'div[role="dialog"] input[type="range"]',
    'div[role="dialog"] input[role="slider"]',
]

COVER_STRIP_CSS = [
    'div[role="dialog"] input[type="range"]',
    'div[role="dialog"] [role="slider"]',
    'div[role="dialog"] [aria-label*="cover" i]',
    'div[role="dialog"] [aria-label*="capa" i]',
    'div[role="dialog"] [aria-label*="frame" i]',
    'div[role="dialog"] [aria-label*="quadro" i]',
]

COVER_THUMB_DRAG_CSS = [
    'div[role="dialog"] [aria-label*="Drag" i]',
    'div[role="dialog"] [aria-label*="arrastar" i]',
    'div[role="dialog"] [role="slider"]',
    'div[role="dialog"] input[type="range"]',
    'div[role="dialog"] [aria-valuenow]',
]
