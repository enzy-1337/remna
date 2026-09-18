"""Общая тема и HTML-хелперы публичного сайта Flux Network (единый стиль с ботом/mini-app/веб-админкой).

Без билд-шага и без Jinja2 — HTML собирается f-строками, как в web_admin.py/public_pages.py.
"""

from __future__ import annotations

import html
from typing import Iterable

from shared.config import Settings

# ---------------------------------------------------------------------------
# Экранирование (тот же контракт, что и в web_admin.py)
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def esc_attr(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

SITE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap');
:root{
  color-scheme: dark;
  --bg:#0A0D11; --bg-flat:#080A0E;
  --card-1:#111820; --card-2:#0F141A; --card-3:#141A21; --card-4:#0D1319;
  --line:rgba(255,255,255,.06); --line-2:rgba(255,255,255,.08);
  --accent:#7B5CFF; --accent-2:#5436C9; --accent-soft:#9D85FF; --accent-softer:#C8B6FF; --accent-mut:#A99FD6;
  --text-1:#EAF0F4; --text-2:#C8D2DA; --text-3:#8A96A3; --text-4:#6E7B86; --text-5:#5E6B76; --text-6:#4A5560;
  --success:#4FD2A0; --warn:#F5B544; --warn-text:#E3C07A; --danger:#FF6B6B; --danger-soft:#FF8A8A; --danger-mut:#A98A8A;
  --tg:#229ED9; --tg-2:#2AABEE; --link:#6AB3F3;
  --radius-lg:20px; --radius-md:14px; --radius-sm:10px;
}
*{box-sizing:border-box;}
html{scroll-behavior:smooth;}
body{
  margin:0;min-height:100vh;background:var(--bg);color:var(--text-1);
  font-family:'Manrope',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--accent-soft);text-decoration:none;transition:color .15s ease;}
a:not(.btn):hover{color:var(--accent-softer);}
.mono{font-family:'JetBrains Mono',monospace;}
img{max-width:100%;}
::selection{background:rgba(123,92,255,.35);}
.hero-bg{background:radial-gradient(110% 60% at 50% -8%,#1a1440 0%,var(--bg) 52%);}
.cabinet-bg{background:radial-gradient(110% 50% at 50% -6%,#17133a 0%,var(--bg) 46%);}

/* --- layout --- */
.shell{max-width:1280px;margin:0 auto;padding:0 24px;}
.shell-wide{max-width:1440px;margin:0 auto;padding:0 32px;}
@media (max-width:720px){ .shell,.shell-wide{padding:0 16px;} }

/* --- animations --- */
@keyframes fadeUp{from{opacity:0;transform:translateY(14px);}to{opacity:1;transform:translateY(0);}}
@keyframes fadeIn{from{opacity:0;}to{opacity:1;}}
@keyframes pulseDot{0%,100%{box-shadow:0 0 0 0 rgba(123,92,255,.55);}50%{box-shadow:0 0 0 6px rgba(123,92,255,0);}}
@keyframes spin{to{transform:rotate(360deg);}}
.fade-up{animation:fadeUp .55s cubic-bezier(.22,1,.36,1) both;}
.fade-up.d1{animation-delay:.06s;} .fade-up.d2{animation-delay:.12s;} .fade-up.d3{animation-delay:.18s;} .fade-up.d4{animation-delay:.24s;}
.fade-in{animation:fadeIn .4s ease both;}
.pulse{animation:pulseDot 2.2s ease-in-out infinite;}

/* --- topbar / pill nav --- */
.topbar-wrap{display:flex;align-items:center;justify-content:center;padding:20px 0;}
.topbar{display:flex;align-items:center;gap:6px;background:rgba(17,24,32,.92);border:1px solid var(--line-2);
  border-radius:999px;padding:8px 10px;backdrop-filter:blur(10px);flex-wrap:wrap;justify-content:center;}
.brand-mark{display:flex;align-items:center;gap:9px;padding:0 12px 0 4px;}
.brand-logo{width:26px;height:26px;border-radius:8px;background:linear-gradient(140deg,var(--accent),var(--accent-2));
  display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.brand-name{font:800 15px Manrope;color:var(--text-1);}
.brand-name b{color:var(--accent);font-weight:800;}
.brand-beta{font:800 9px Manrope;color:var(--accent-soft);background:rgba(123,92,255,.16);padding:3px 6px;border-radius:5px;letter-spacing:.06em;}
.topbar-links{display:flex;align-items:center;gap:2px;flex-wrap:wrap;justify-content:center;}
.topbar-link{display:flex;align-items:center;gap:6px;font:600 12.5px Manrope;color:var(--text-3);padding:8px 14px;
  border-radius:999px;transition:.15s ease;white-space:nowrap;}
.topbar-link:hover{color:var(--text-2);}
.topbar-link.active{color:var(--accent-softer);background:rgba(123,92,255,.16);border:1px solid rgba(123,92,255,.3);font-weight:700;}
.topbar-sep{width:1px;height:22px;background:var(--line-2);margin:0 6px;}
.topbar-right{display:flex;align-items:center;gap:6px;}
.pill-btn{display:flex;align-items:center;gap:8px;background:var(--accent);border-radius:999px;padding:9px 20px;
  font:800 13px Manrope;color:#fff;border:0;cursor:pointer;transition:transform .15s ease,box-shadow .15s ease;}
.pill-btn:hover{transform:translateY(-1px);box-shadow:0 14px 30px -14px rgba(123,92,255,.85);}
.icon-btn{width:30px;height:30px;border-radius:50%;background:var(--card-3);display:flex;align-items:center;justify-content:center;
  color:var(--text-3);border:0;cursor:pointer;transition:.15s ease;flex-shrink:0;}
.icon-btn:hover{color:var(--text-1);background:#1a2129;}
.balance-chip{display:flex;align-items:center;gap:7px;background:var(--card-3);border-radius:999px;padding:6px 11px 6px 9px;color:var(--text-1);
  font:700 12.5px Manrope;}
.avatar-circle{border-radius:50%;background:linear-gradient(140deg,var(--accent),var(--accent-2));
  display:flex;align-items:center;justify-content:center;font:800 13px Manrope;color:#fff;flex-shrink:0;}

/* --- buttons --- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;border-radius:13px;font:800 14px Manrope;
  padding:13px 22px;border:0;cursor:pointer;transition:transform .15s ease,box-shadow .15s ease,opacity .15s ease,background .15s ease;text-decoration:none;}
.btn:active{transform:scale(.98);}
.btn-lg{padding:16px 28px;font-size:15px;border-radius:14px;}
.btn-block{width:100%;}
.btn-primary{background:var(--accent);color:#fff;box-shadow:0 16px 36px -16px rgba(123,92,255,.85);}
.btn-primary:hover{box-shadow:0 20px 44px -14px rgba(123,92,255,.95);transform:translateY(-1px);}
.btn-secondary{background:var(--accent);color:#fff;}
.btn-outline{background:var(--card-4);border:1px solid var(--line-2);color:var(--text-2);}
.btn-outline:hover{border-color:rgba(123,92,255,.4);color:var(--text-1);}
.btn-danger-outline{background:transparent;border:1px solid rgba(255,107,107,.3);color:var(--danger-soft);}
.btn-danger-outline:hover{background:rgba(255,107,107,.08);}
.btn-tg{background:var(--tg);color:#fff;box-shadow:0 14px 34px -12px rgba(34,158,217,.8);}
.btn-google{background:#fff;color:#1f2429;}
.btn-disabled{opacity:.45;cursor:not-allowed;pointer-events:none;}
.btn-sm{padding:9px 16px;font-size:12.5px;border-radius:11px;}
.link-btn{background:transparent;border:0;color:var(--accent-soft);font:700 12px Manrope;cursor:pointer;}
.link-btn:hover{color:var(--accent-softer);}

/* --- cards --- */
.card{background:var(--card-1);border:1px solid var(--line);border-radius:var(--radius-md);padding:22px 24px;}
.card-accent{border-color:rgba(123,92,255,.22);}
.card-soft{background:rgba(123,92,255,.07);border:1px solid rgba(123,92,255,.25);}
.card-danger{background:rgba(255,107,107,.06);border:1px solid rgba(255,107,107,.22);}
.section-label{display:flex;align-items:center;gap:9px;font:800 12px Manrope;color:#9AA7B2;letter-spacing:.07em;text-transform:uppercase;}

/* --- badges --- */
.badge{display:inline-flex;align-items:center;gap:5px;font:700 11px Manrope;padding:4px 9px;border-radius:6px;}
.badge-success{background:rgba(79,210,160,.13);color:var(--success);}
.badge-warn{background:rgba(245,181,68,.13);color:var(--warn);}
.badge-danger{background:rgba(255,107,107,.14);color:var(--danger-soft);border:1px solid rgba(255,107,107,.3);}
.badge-purple{background:rgba(123,92,255,.16);color:var(--accent-softer);border:1px solid rgba(123,92,255,.3);}
.badge-neutral{background:rgba(255,255,255,.06);color:var(--text-3);}

/* --- progress --- */
.bar-track{height:7px;background:var(--card-4);border-radius:4px;overflow:hidden;}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-soft));border-radius:4px;transition:width .5s ease;}
.ring{border-radius:50%;position:relative;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.ring-inner{position:absolute;border-radius:50%;background:var(--card-1);display:flex;flex-direction:column;align-items:center;justify-content:center;}

/* --- forms --- */
.field-label{font:600 12px Manrope;color:var(--text-3);margin-bottom:7px;display:block;}
.input,select.input,textarea.input{width:100%;background:var(--card-3);border:1px solid var(--line-2);color:var(--text-1);
  border-radius:12px;padding:13px 14px;font:600 14px Manrope;outline:none;transition:border-color .15s ease;}
.input::placeholder{color:var(--text-5);}
.input:focus{border-color:rgba(123,92,255,.55);}
.otp-row{display:flex;gap:10px;justify-content:center;}
.otp-box{width:50px;height:60px;border-radius:12px;background:var(--card-3);border:1px solid var(--line-2);
  display:flex;align-items:center;justify-content:center;font:700 24px 'JetBrains Mono',monospace;color:var(--text-1);
  text-align:center;transition:border-color .15s ease;}
.otp-box:focus{outline:none;border-color:rgba(123,92,255,.7);}
.otp-box.filled{border-color:rgba(123,92,255,.4);}

/* --- toggle switch --- */
.toggle{position:relative;width:42px;height:24px;border-radius:999px;background:var(--card-4);border:1px solid var(--line-2);
  cursor:pointer;flex-shrink:0;transition:background .2s ease;}
.toggle .knob{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:var(--text-4);
  transition:transform .2s ease,background .2s ease;}
.toggle.on{background:rgba(123,92,255,.35);border-color:rgba(123,92,255,.5);}
.toggle.on .knob{transform:translateX(18px);background:var(--accent);}

/* --- tabs / segmented --- */
.tabs{display:inline-flex;background:var(--card-1);border-radius:13px;padding:5px;gap:2px;}
.tab{padding:9px 18px;border-radius:10px;font:700 13px Manrope;color:var(--text-3);cursor:pointer;border:0;background:transparent;transition:.15s ease;}
.tab.active{background:var(--accent);color:#fff;}

/* --- accordion (FAQ) --- */
.acc-item{border-radius:14px;transition:background .2s ease,border-color .2s ease;}
.acc-item.open{background:rgba(123,92,255,.1);border:1px solid rgba(123,92,255,.3);}
.acc-head{display:flex;align-items:center;gap:12px;padding:15px 16px;cursor:pointer;}
.acc-title{flex:1;font:700 14px Manrope;color:var(--text-1);}
.acc-chevron{transition:transform .25s ease;color:var(--text-3);}
.acc-item.open .acc-chevron{transform:rotate(180deg);color:var(--accent-soft);}
.acc-body{max-height:0;overflow:hidden;transition:max-height .3s ease;padding:0 16px;}
.acc-item.open .acc-body{max-height:400px;padding:0 16px 16px 46px;}
.acc-body p{margin:0;font:500 13.5px Manrope;color:var(--text-3);line-height:1.6;}

/* --- chat / tickets --- */
.chat-scroll{display:flex;flex-direction:column;gap:10px;overflow-y:auto;}
.msg-row{display:flex;gap:10px;max-width:80%;}
.msg-row.me{margin-left:auto;flex-direction:row-reverse;}
.msg-bubble{padding:11px 14px;border-radius:14px 14px 14px 4px;background:var(--card-4);font:500 13.5px Manrope;color:var(--text-2);line-height:1.5;}
.msg-row.me .msg-bubble{background:rgba(123,92,255,.12);border:1px solid rgba(123,92,255,.22);border-radius:14px 14px 4px 14px;color:var(--text-1);}
.msg-meta{font:600 10.5px Manrope;color:var(--text-4);margin-top:4px;}
.composer{display:flex;align-items:center;gap:10px;background:var(--card-4);border:1px solid var(--line-2);border-radius:13px;padding:10px 12px;}
.composer input{flex:1;background:transparent;border:0;color:var(--text-1);font:500 14px Manrope;outline:none;}
.composer input::placeholder{color:var(--text-5);}

/* --- misc --- */
.divider{height:1px;background:var(--line);}
.vdivider{width:1px;background:var(--line-2);}
.opacity-60{opacity:.6;}
.grid-auto{display:grid;gap:16px;}
.scroll-x{overflow-x:auto;}
.skeleton{background:linear-gradient(90deg,var(--card-3) 25%,var(--card-2) 37%,var(--card-3) 63%);background-size:400% 100%;
  animation:shine 1.4s ease infinite;border-radius:8px;}
@keyframes shine{0%{background-position:100% 50%;}100%{background-position:0 50%;}}

footer.site-footer{border-top:1px solid var(--line);padding:26px 0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px;}
.footer-links{display:flex;gap:24px;font:500 13px Manrope;color:var(--text-4);flex-wrap:wrap;}

@media (max-width:900px){
  .cols-2{grid-template-columns:1fr !important;}
  .topbar-links{order:3;width:100%;justify-content:center;padding-top:6px;border-top:1px solid var(--line-2);margin-top:6px;}
  .hide-mobile{display:none !important;}
}
"""


# ---------------------------------------------------------------------------
# Мелкие SVG-иконки (единый набор stroke-иконок из макета)
# ---------------------------------------------------------------------------


def icon(name: str, *, size: int = 16, color: str = "currentColor", stroke: float = 1.9) -> str:
    paths = {
        "shield": '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
        "bolt": '<path d="M13 2L4 14h6l-1 8 9-12h-6z" fill="{c}" stroke="none"/>',
        "lock": '<rect x="4" y="10" width="16" height="11" rx="2.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
        "devices": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/>',
        "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.4 2.5 2.4 15 0 18M12 3c-2.4 2.5-2.4 15 0 18"/>',
        "home": '<path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z"/>',
        "people": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5.5a3 3 0 0 1 0 5.5M18 20a5.5 5.5 0 0 0-3-4.6"/>',
        "send-tg": '<path d="M21 4L3 11l7 3 3 7z"/>',
        "tickets": '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M8 6v12M16 6v12"/>',
        "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.6 2.6 0 1 1 3.4 2.5c-.6.2-.9.7-.9 1.3v.4M12 17h.01"/>',
        "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 14a2 2 0 1 1 0-4 1.6 1.6 0 0 0 1.3-2.6l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 10 3a2 2 0 1 1 4 0 1.6 1.6 0 0 0 2.6 1.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.6 1.6 0 0 0 21 10a2 2 0 1 1 0 4 1.6 1.6 0 0 0-1.6 1z"/>',
        "bell": '<path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6"/><path d="M10.5 20a2 2 0 0 0 3 0"/>',
        "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
        "link": '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
        "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/>',
        "coupon": '<rect x="3" y="7" width="18" height="12" rx="2"/><path d="M8 7V5h8v2"/>',
        "chat": '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
        "chevron-right": '<path d="M9 6l6 6-6 6"/>',
        "chevron-down": '<path d="M6 9l6 6 6-6"/>',
        "chevron-up": '<path d="M6 15l6-6 6 6"/>',
        "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
        "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
        "check-circle": '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 5-5.5"/>',
        "bar-chart": '<path d="M3 12h4l2-5 3 10 2-5h5"/>',
        "headset": '<path d="M4 13a8 8 0 0 1 16 0"/><rect x="3" y="13" width="4" height="6" rx="1.5"/><rect x="17" y="13" width="4" height="6" rx="1.5"/><path d="M20 19a4 4 0 0 1-4 3h-2"/>',
        "monitor": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
        "shield-check": '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4.5"/>',
        "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
        "star": '<path d="M12 2l2.9 6.2 6.7.8-5 4.6 1.4 6.6L12 16.9 5.9 20.2 7.3 13.6l-5-4.6 6.7-.8z"/>',
        "wallet": '<rect x="3" y="6" width="18" height="12" rx="2.5"/><path d="M3 10h18"/>',
        "wifi": '<path d="M2 8.5a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a6 6 0 0 1 7 0"/><circle cx="12" cy="19" r="1" fill="{c}" stroke="none"/>',
        "trend-up": '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
        "paperclip": '<path d="M21 11l-9.5 9.5a4 4 0 0 1-5.6-5.6L15.5 5.3a2.7 2.7 0 0 1 3.8 3.8L10 18.4a1.3 1.3 0 0 1-1.9-1.9l8-8"/>',
        "smile": '<circle cx="12" cy="12" r="9"/><path d="M8.5 14s1.2 2 3.5 2 3.5-2 3.5-2M9 9h.01M15 9h.01"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "qr": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM19 14h2M14 19h2M19 19h2"/>',
        "gift": '<rect x="3" y="8" width="18" height="13" rx="1.5"/><path d="M12 8v13M3 12h18"/><path d="M12 8c-1.5 0-3-.9-3-2.5S10.3 3 12 3s3 .9 3 2.5S13.5 8 12 8zm0 0c1.5 0 3-.9 3-2.5S13.7 3 12 3"/>',
        "x": '<path d="M18 6L6 18M6 6l12 12"/>',
    }
    p = paths.get(name, "").replace("{c}", color)
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{p}</svg>'
    )


def tg_logo_svg(size: int = 16, color: str = "#fff") -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="{color}">'
        '<path d="M21.9 4.3l-3.3 15.6c-.25 1.1-.9 1.37-1.82.85l-5.04-3.7-2.43 2.34c-.27.27-.5.5-1 .5l.36-5.1L18 5.6c.4-.36-.08-.56-.62-.2L6.66 12.3l-4.96-1.55c-1.08-.34-1.1-1.08.23-1.6l19.4-7.47c.9-.34 1.68.2 1.39 1.62z"/></svg>'
    )


def google_logo_svg(size: int = 18) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 48 48">'
        '<path fill="#4285F4" d="M45 24c0-1.6-.15-3.1-.4-4.6H24v9h11.8c-.5 2.8-2.1 5.1-4.4 6.7v5.5h7.1C42.6 36.7 45 30.9 45 24z"/>'
        '<path fill="#34A853" d="M24 46c5.9 0 10.9-2 14.5-5.4l-7.1-5.5c-2 1.3-4.5 2.1-7.4 2.1-5.7 0-10.5-3.8-12.2-9H4.5v5.7C8.1 41.2 15.4 46 24 46z"/>'
        '<path fill="#FBBC05" d="M11.8 28.2c-.4-1.3-.7-2.7-.7-4.2s.3-2.9.7-4.2v-5.7H4.5C2.9 17.3 2 20.5 2 24s.9 6.7 2.5 9.9l7.3-5.7z"/>'
        '<path fill="#EA4335" d="M24 10.1c3.2 0 6 1.1 8.3 3.2l6.2-6.2C34.9 3.6 29.9 1.5 24 1.5 15.4 1.5 8.1 6.3 4.5 14.1l7.3 5.7c1.7-5.2 6.5-9.7 12.2-9.7z"/></svg>'
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def page(*, title: str, body: str, extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<style>{SITE_CSS}</style>
{extra_head}
</head>
<body>
{body}
</body>
</html>"""


def brand_mark(*, size: str = "md") -> str:
    logo = "26px" if size == "md" else "24px"
    return f"""
<div class="brand-mark">
  <div class="brand-logo" style="width:{logo};height:{logo};">{icon('shield', size=15 if size == 'md' else 14, color='#fff', stroke=2.4)}</div>
  <span class="brand-name">Flux<b>VPN</b></span>
  <span class="brand-beta">BETA</span>
</div>"""


def public_topbar(*, active: str = "") -> str:
    items = [
        ("Возможности", "/#features", "features"),
        ("Тарифы", "/#pricing", "pricing"),
        ("Приложения", "/#apps", "apps"),
        ("Помощь", "/app/help", "help"),
    ]
    links = "".join(
        f'<a class="topbar-link{" active" if key == active else ""}" href="{href}">{esc(label)}</a>'
        for label, href, key in items
    )
    return f"""
<div class="topbar-wrap fade-up">
  <div class="topbar">
    {brand_mark()}
    <div class="topbar-links">{links}</div>
    <div class="topbar-sep hide-mobile"></div>
    <a class="pill-btn" href="/login">{icon('arrow-right', size=14, color='#fff', stroke=2.4)}<span>Войти</span></a>
  </div>
</div>"""


def app_topbar(*, active: str, balance_rub: str, unread_tickets: int, initial: str) -> str:
    items = [
        ("Главная", "/app", "home", "home"),
        ("Рефералка", "/app/referrals", "people", "referrals"),
        ("Тикеты", "/app/tickets", "tickets", "tickets"),
        ("Помощь", "/app/help", "help", "help"),
    ]
    links = "".join(
        f'<a class="topbar-link{" active" if key == active else ""}" href="{href}">'
        f'{icon(ic, size=13)}<span>{esc(label)}</span></a>'
        for label, href, ic, key in items
    )
    badge = (
        f'<div style="position:absolute;top:-2px;right:-2px;min-width:15px;height:15px;border-radius:8px;'
        f'background:var(--accent);display:flex;align-items:center;justify-content:center;font:800 9px Manrope;color:#fff;padding:0 4px;">'
        f"{unread_tickets}</div>"
        if unread_tickets > 0
        else ""
    )
    return f"""
<div class="topbar-wrap fade-up">
  <div class="topbar">
    {brand_mark()}
    <div class="topbar-links">{links}</div>
    <div class="topbar-sep hide-mobile"></div>
    <div style="display:flex;align-items:center;gap:6px;">
      <a class="balance-chip" href="/app#topup" title="Пополнить баланс">{icon('wallet', size=14, color='var(--accent-soft)')}<span>{esc(balance_rub)} ₽</span><span style="color:var(--accent);font-weight:800;">+</span></a>
      <a class="icon-btn" href="/app/tickets" style="position:relative;" title="Тикеты">{icon('tickets', size=15)}{badge}</a>
      <a class="avatar-circle" href="/app/profile" style="width:30px;height:30px;" title="Профиль">{esc(initial)}</a>
      <form method="post" action="/logout" style="margin:0;">
        <button type="submit" class="icon-btn" title="Выйти">{icon('logout', size=15)}</button>
      </form>
    </div>
  </div>
</div>"""


def site_footer() -> str:
    return f"""
<footer class="site-footer shell">
  <div style="display:flex;align-items:center;gap:10px;">
    <div style="width:22px;height:22px;border-radius:7px;background:linear-gradient(140deg,var(--accent),var(--accent-2));"></div>
    <span style="font:700 13px Manrope;color:var(--text-3);">© 2026 Flux Network</span>
  </div>
  <div class="footer-links">
    <a href="/legal/privacy">Политика конфиденциальности</a>
    <a href="/legal/offer">Публичная оферта</a>
    <a href="/legal/refund">Политика возврата</a>
  </div>
</footer>"""


def copy_field(*, value: str, label: str = "") -> str:
    lbl = f'<div class="field-label">{esc(label)}</div>' if label else ""
    vid = f"copy-{abs(hash(value)) % 10**8}"
    return f"""
{lbl}
<div class="input" id="{vid}" style="display:flex;align-items:center;gap:10px;cursor:pointer;" data-copy-field data-copy-value="{esc_attr(value)}">
  <span class="mono" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-2);">{esc(value)}</span>
  {icon('copy', size=15, color='var(--accent-soft)')}
</div>"""


COPY_JS = """
document.addEventListener('click', function(e){
  var el = e.target.closest('[data-copy-field]');
  if(!el) return;
  var val = el.getAttribute('data-copy-value') || '';
  navigator.clipboard && navigator.clipboard.writeText(val).then(function(){
    var icn = el.querySelector('svg');
    if(icn){ var prev = el.style.borderColor; el.style.borderColor='rgba(79,210,160,.6)'; setTimeout(function(){ el.style.borderColor = prev; }, 700); }
  });
});
"""

ACCORDION_JS = """
document.addEventListener('click', function(e){
  var head = e.target.closest('[data-acc-head]');
  if(!head) return;
  var item = head.closest('[data-acc-item]');
  if(!item) return;
  item.classList.toggle('open');
});
"""

TABS_JS = """
document.addEventListener('click', function(e){
  var tab = e.target.closest('[data-tab]');
  if(!tab) return;
  var group = tab.closest('[data-tab-group]');
  if(!group) return;
  var target = tab.getAttribute('data-tab');
  group.querySelectorAll('[data-tab]').forEach(function(t){ t.classList.toggle('active', t === tab); });
  group.querySelectorAll('[data-tab-panel]').forEach(function(p){ p.hidden = p.getAttribute('data-tab-panel') !== target; });
});
"""

TOGGLE_JS = """
document.addEventListener('click', function(e){
  var t = e.target.closest('[data-toggle-form]');
  if(!t) return;
  var form = document.getElementById(t.getAttribute('data-toggle-form'));
  if(form) form.requestSubmit();
});
"""


def fmt_money(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    s = f"{n:,.0f}".replace(",", " ")
    return s


def bot_deep_link(settings: Settings) -> str:
    uname = (settings.bot_username or "").strip().lstrip("@")
    return f"https://t.me/{uname}" if uname else "#"


def support_handle_link(settings: Settings) -> str:
    return bot_deep_link(settings)
