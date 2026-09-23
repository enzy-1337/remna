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
  display:flex;align-items:center;justify-content:center;font:800 13px Manrope;color:#fff;flex-shrink:0;position:relative;overflow:hidden;}
.avatar-circle .av-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:inherit;}

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
.acc-item{border-radius:14px;border:1px solid transparent;transition:background .25s ease,border-color .25s ease;margin-bottom:8px;}
.acc-item:last-child{margin-bottom:0;}
.acc-item.open{background:rgba(123,92,255,.1);border-color:rgba(123,92,255,.3);}
.acc-head{display:flex;align-items:center;gap:12px;padding:15px 16px;cursor:pointer;}
.acc-title{flex:1;font:700 14px Manrope;color:var(--text-1);}
.acc-chevron{transition:transform .35s cubic-bezier(.22,1,.36,1);color:var(--text-3);flex-shrink:0;}
.acc-item.open .acc-chevron{transform:rotate(180deg);color:var(--accent-soft);}
.acc-body{display:grid;grid-template-rows:0fr;transition:grid-template-rows .35s cubic-bezier(.22,1,.36,1);}
.acc-item.open .acc-body{grid-template-rows:1fr;}
.acc-body>div{overflow:hidden;min-height:0;}
.acc-body-inner{padding:0 16px 16px 46px;}
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
.modal-overlay{position:fixed;inset:0;z-index:80;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.6);padding:16px;}
.modal-overlay.open{display:flex;}
.toast-stack{position:fixed;bottom:20px;right:20px;z-index:200;display:flex;flex-direction:column;gap:8px;max-width:calc(100vw - 32px);}
.toast{background:var(--card-2);border:1px solid var(--line-2);border-left:3px solid var(--accent-soft);border-radius:12px;padding:12px 16px;font:600 13px Manrope;color:var(--text-1);box-shadow:0 20px 50px -15px rgba(0,0,0,.6);max-width:340px;animation:fadeUp .3s ease both;}
.toast.success{border-left-color:var(--success);}
.toast.error{border-left-color:var(--danger);color:var(--danger-soft);}
.vdivider{width:1px;background:var(--line-2);}
.opacity-60{opacity:.6;}
.grid-auto{display:grid;gap:16px;}
.scroll-x{overflow-x:auto;}
.skeleton{background:linear-gradient(90deg,var(--card-3) 25%,var(--card-2) 37%,var(--card-3) 63%);background-size:400% 100%;
  animation:shine 1.4s ease infinite;border-radius:8px;}
@keyframes shine{0%{background-position:100% 50%;}100%{background-position:0 50%;}}

footer.site-footer{border-top:1px solid var(--line);margin-top:40px;padding:26px 0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px;}
.footer-links{display:flex;gap:24px;font:500 13px Manrope;color:var(--text-4);flex-wrap:wrap;}

@media (max-width:900px){
  .cols-2{grid-template-columns:1fr !important;}
  .topbar{border-radius:22px;}
  .topbar-links{order:3;width:100%;justify-content:center;padding-top:6px;border-top:1px solid var(--line-2);margin-top:6px;}
  .hide-mobile{display:none !important;}
}
@media (max-width:640px){
  body{font-size:14px;}
  .hero-bg [style*="padding:96px 0 0"]{padding-top:56px !important;}
  #features{margin-top:56px !important;}
  #pricing{margin-top:40px !important;padding:24px 20px !important;flex-direction:column;align-items:stretch !important;}
  .card{padding:16px !important;}
  .topbar{gap:4px;padding:6px 8px;}
  .topbar-link span{display:none;}
  .topbar-link{padding:9px;}
  .brand-name{font-size:13px;}
  .balance-chip span{font-size:12px;}
  h1,[style*="font:800 clamp"]{line-height:1.15;}
  .modal-overlay>div{max-width:100% !important;}
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
        "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
        "arrow-left": '<path d="M19 12H5M11 18l-6-6 6-6"/>',
        "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
        "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
        "stop": '<rect x="7" y="7" width="10" height="10" rx="2" fill="{c}" stroke="none"/>',
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


_TOAST_JS = """
window.remnaToast = function(kind, text){
  var stack = document.getElementById('toast-stack');
  if(!stack){ stack = document.createElement('div'); stack.id='toast-stack'; stack.className='toast-stack'; document.body.appendChild(stack); }
  var el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = text;
  stack.appendChild(el);
  setTimeout(function(){
    el.style.transition = 'opacity .3s ease, transform .3s ease';
    el.style.opacity = '0'; el.style.transform = 'translateY(6px)';
    setTimeout(function(){ el.remove(); }, 300);
  }, 3800);
};
(function(){
  try {
    var u = new URL(window.location.href);
    var n = u.searchParams.get('n');
    var err = u.searchParams.get('err');
    var map = {
      promo_ok: 'Промокод активирован.',
      promo_err: 'Не удалось активировать промокод — проверьте код.',
      '2fa_on': 'Двухфакторная аутентификация включена.',
      '2fa_off': 'Двухфакторная аутентификация выключена.',
      sessions_revoked: 'Сессии завершены.',
      google_linked: 'Google привязан.',
      email_linked: 'Почта привязана.',
      mkt_on: 'Рассылка на почту включена.',
      mkt_off: 'Рассылка отключена. Чеки и уведомления о подписке продолжат приходить.'
    };
    if (n && map[n]) window.remnaToast('success', map[n]);
    if (err) window.remnaToast('error', err);
    if (n || err) {
      u.searchParams.delete('n');
      u.searchParams.delete('err');
      var qs = u.searchParams.toString();
      window.history.replaceState({}, '', u.pathname + (qs ? '?' + qs : ''));
    }
  } catch (e) {}
})();
"""


def page(*, title: str, body: str, extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{f'<link rel="icon" href="{esc(site_logo_url())}">' if site_logo_url() else ''}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap">
<style>{SITE_CSS}</style>
{extra_head}
</head>
<body>
{body}
<script>{_TOAST_JS}</script>
</body>
</html>"""


def site_logo_url() -> str:
    """Логотип сайта — тот же, что в web-admin (ADMIN_PANEL_LOGO_URL). Пусто — встроенный значок."""
    from shared.config import get_settings

    url = (get_settings().admin_panel_logo_url or "").strip()
    if url.startswith(("https://", "http://", "/")):
        return url
    return ""


def _logo_inner(px: int, icon_size: int) -> str:
    url = site_logo_url()
    if url:
        return (
            f'<img src="{esc(url)}" alt="Flux" width="{px}" height="{px}" '
            f'style="width:86%;height:86%;object-fit:contain;display:block;"/>'
        )
    return icon('shield', size=icon_size, color='#fff', stroke=2.4)


def _logo_box_style() -> str:
    # С картинкой — без фиолетовой подложки, чтобы не просвечивала по краям прозрачного PNG.
    return "background:none;display:flex;align-items:center;justify-content:center;" if site_logo_url() else ""


def brand_mark(*, size: str = "md") -> str:
    logo = "26px" if size == "md" else "24px"
    px = 26 if size == "md" else 24
    return f"""
<div class="brand-mark">
  <div class="brand-logo" style="width:{logo};height:{logo};{_logo_box_style()}">{_logo_inner(px, 15 if size == 'md' else 14)}</div>
  <span class="brand-name">Flux<b>VPN</b></span>
  <span class="brand-beta">BETA</span>
</div>"""


def avatar_img() -> str:
    """Фото профиля Telegram текущего пользователя поверх буквы-заглушки (если фото нет — остаётся буква)."""
    return '<img class="av-img" src="/app/avatar" alt="" loading="lazy" onerror="this.remove()">'


def public_topbar(*, active: str = "", user_initial: str | None = None) -> str:
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
<div class="topbar-wrap fade-in">
  <div class="topbar">
    {brand_mark()}
    <div class="topbar-links">{links}</div>
    <div class="topbar-sep hide-mobile"></div>
    {_public_account_block(user_initial)}
  </div>
</div>"""


def _public_account_block(user_initial: str | None) -> str:
    """Гость — «Войти»; вошедший — «В панель», аватарка и выход."""
    if not user_initial:
        return f'<a class="pill-btn" href="/login">{icon("arrow-right", size=14, color="#fff", stroke=2.4)}<span>Войти</span></a>'
    return f"""<div style="display:flex;align-items:center;gap:8px;">
      <a class="pill-btn" href="/app">{icon('home', size=14, color='#fff', stroke=2.2)}<span>В панель</span></a>
      <a class="avatar-circle" href="/app/profile" style="width:34px;height:34px;" title="Профиль">{esc(user_initial)}{avatar_img()}</a>
      <form method="post" action="/logout" style="margin:0;">
        <button type="submit" class="icon-btn" style="width:34px;height:34px;" title="Выйти">{icon('logout', size=15)}</button>
      </form>
    </div>"""


def app_topbar(*, active: str, balance_rub: str, unread_tickets: int, initial: str) -> str:
    items = [
        ("Главная", "/app", "home", "home"),
        ("Подписка", "/app/subscription", "shield", "subscription"),
        ("Рефералка", "/app/referrals", "people", "referrals"),
        ("Тикеты", "/app/tickets", "tickets", "tickets"),
        ("Помощь", "/app/help", "help", "help"),
    ]
    links = "".join(
        f'<a class="topbar-link{" active" if key == active else ""}" href="{href}">'
        f'{icon(ic, size=13)}<span>{esc(label)}</span></a>'
        for label, href, ic, key in items
    )
    return f"""
<div class="topbar-wrap fade-in">
  <div class="topbar">
    {brand_mark()}
    <div class="topbar-links">{links}</div>
    <div class="topbar-sep hide-mobile"></div>
    <div style="display:flex;align-items:center;gap:6px;">
      <button type="button" class="balance-chip" style="border:0;cursor:pointer;" data-open-topup title="Пополнить баланс">{icon('wallet', size=14, color='var(--accent-soft)')}<span>{esc(balance_rub)} ₽</span><span style="color:var(--accent);font-weight:800;">+</span></button>
      <div style="position:relative;">
        <button type="button" class="icon-btn" style="position:relative;" data-open-notifs title="Уведомления">
          {icon('bell', size=15)}
          <span id="notifs-badge" hidden style="position:absolute;top:-2px;right:-2px;min-width:15px;height:15px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;font:800 9px Manrope;color:#fff;padding:0 4px;"></span>
        </button>
        <div id="notifs-dropdown" hidden style="position:absolute;top:calc(100% + 10px);right:0;width:320px;max-width:calc(100vw - 40px);background:var(--card-2);border:1px solid var(--line-2);border-radius:16px;box-shadow:0 30px 60px -20px rgba(0,0,0,.7);z-index:70;overflow:hidden;">
          <div style="padding:14px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;">
            <span style="font:800 14px Manrope;color:var(--text-1);">Уведомления</span>
            <button type="button" class="icon-btn" style="width:24px;height:24px;" data-close-notifs>{icon('x', size=12)}</button>
          </div>
          <div id="notifs-list" style="max-height:360px;overflow-y:auto;">
            <div style="padding:32px 16px;text-align:center;">
              <div style="width:36px;height:36px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;margin:0 auto;">{icon('bell', size=16, color='var(--text-4)')}</div>
              <div style="font:600 12.5px Manrope;color:var(--text-4);margin-top:10px;">Загрузка…</div>
            </div>
          </div>
        </div>
      </div>
      <a class="avatar-circle" href="/app/profile" style="width:30px;height:30px;" title="Профиль">{esc(initial)}{avatar_img()}</a>
      <form method="post" action="/logout" style="margin:0;">
        <button type="submit" class="icon-btn" title="Выйти">{icon('logout', size=15)}</button>
      </form>
    </div>
  </div>
</div>

<div id="topup-modal" class="modal-overlay">
  <div class="fade-in" style="width:100%;max-width:380px;background:var(--card-2);border:1px solid var(--line-2);border-radius:18px;padding:24px;">
    <div style="display:flex;align-items:center;gap:12px;">
      <div style="width:38px;height:38px;border-radius:11px;background:rgba(123,92,255,.14);display:flex;align-items:center;justify-content:center;">{icon('wallet', size=18, color='var(--accent-soft)')}</div>
      <div>
        <div style="font:800 16px Manrope;color:var(--text-1);">Пополнение баланса</div>
        <div style="font:500 11px Manrope;color:var(--text-4);">Текущий баланс: {esc(balance_rub)} ₽</div>
      </div>
    </div>
    <form method="post" action="/app/topup" style="margin-top:18px;">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));gap:8px;">
        {"".join(f'<button type="button" class="btn btn-outline btn-sm" data-amount-btn data-amount="{a}">{a} ₽</button>' for a in (100, 200, 500, 1000))}
      </div>
      <input class="input mono" name="amount" id="topup-amount" placeholder="Своя сумма, ₽" inputmode="numeric" style="margin-top:10px;" required/>
      <div style="display:flex;gap:10px;margin-top:14px;">
        <button type="button" class="btn btn-outline btn-block" data-close-topup>Отмена</button>
        <button type="submit" class="btn btn-primary btn-block">Пополнить</button>
      </div>
    </form>
  </div>
</div>
{f'''<div id="support-fab" style="position:fixed;right:20px;bottom:20px;z-index:90;">
  <div id="support-popover" hidden style="position:absolute;bottom:64px;right:0;width:min(280px,calc(100vw - 40px));background:var(--card-2);border:1px solid var(--line-2);border-radius:16px;box-shadow:0 30px 60px -20px rgba(0,0,0,.7);padding:16px;">
    <div style="font:800 14px Manrope;color:var(--text-1);">Нужна помощь?</div>
    <div style="font:500 12px Manrope;color:var(--text-4);margin-top:4px;">Напишите нам — ответим в тикете личного кабинета.</div>
    <a href="/app/tickets" class="btn btn-primary btn-block" style="margin-top:12px;">{icon('chat', size=15, color='#fff')}<span>Открыть тикеты</span></a>
  </div>
  <button type="button" id="support-fab-btn" class="icon-btn" style="width:52px;height:52px;background:var(--accent);color:#fff;box-shadow:0 16px 40px -12px rgba(123,92,255,.9);">{icon('chat', size=20, color='#fff')}</button>
</div>
<script>
(function(){{
  var fabBtn = document.getElementById('support-fab-btn');
  var pop = document.getElementById('support-popover');
  if (fabBtn && pop) {{
    fabBtn.addEventListener('click', function(e){{ e.stopPropagation(); pop.hidden = !pop.hidden; }});
    document.addEventListener('click', function(e){{ if (!pop.hidden && !pop.contains(e.target) && e.target !== fabBtn) pop.hidden = true; }});
  }}
}})();
</script>''' if active != "tickets" else ""}
<script>
(function(){{
  var topupModal = document.getElementById('topup-modal');
  var topupInput = document.getElementById('topup-amount');
  document.querySelectorAll('[data-open-topup]').forEach(function(b){{ b.addEventListener('click', function(){{ topupModal.classList.add('open'); topupInput.focus(); }}); }});
  document.querySelectorAll('[data-close-topup]').forEach(function(b){{ b.addEventListener('click', function(){{ topupModal.classList.remove('open'); }}); }});
  topupModal.addEventListener('click', function(e){{ if (e.target === this) this.classList.remove('open'); }});
  document.querySelectorAll('[data-amount-btn]').forEach(function(b){{
    b.addEventListener('click', function(){{
      topupInput.value = b.getAttribute('data-amount');
      document.querySelectorAll('[data-amount-btn]').forEach(function(x){{ x.classList.remove('btn-primary'); x.classList.add('btn-outline'); }});
      b.classList.remove('btn-outline'); b.classList.add('btn-primary');
    }});
  }});
  var notifsBtn = document.querySelector('[data-open-notifs]');
  var notifsDd = document.getElementById('notifs-dropdown');
  var notifsBadge = document.getElementById('notifs-badge');
  var notifsList = document.getElementById('notifs-list');
  function escHtml(s){{ var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }}
  function renderNotifs(items){{
    if (!items || !items.length) {{
      notifsList.innerHTML = '<div style="padding:32px 16px;text-align:center;">'
        + '<div style="width:36px;height:36px;border-radius:10px;background:var(--card-3);display:flex;align-items:center;justify-content:center;margin:0 auto;">{icon('bell', size=16, color='var(--text-4)')}</div>'
        + '<div style="font:600 12.5px Manrope;color:var(--text-4);margin-top:10px;">Пока нет уведомлений</div></div>';
      return;
    }}
    notifsList.innerHTML = items.map(function(n){{
      return '<div style="padding:12px 16px;border-bottom:1px solid var(--line);' + (n.unread ? 'background:rgba(123,92,255,.08);' : '') + '">'
        + '<div style="display:flex;align-items:center;gap:7px;font:700 12.5px Manrope;color:var(--text-1);">'
        + (n.unread ? '<span style="width:7px;height:7px;border-radius:50%;background:var(--accent);flex-shrink:0;"></span>' : '')
        + escHtml(n.title) + '</div>'
        + '<div style="font:500 12px Manrope;color:var(--text-3);margin-top:4px;line-height:1.5;">' + n.body_html + '</div>'
        + '<div style="font:600 10.5px Manrope;color:var(--text-4);margin-top:6px;">' + escHtml(n.sent_at) + '</div>'
        + '</div>';
    }}).join('');
  }}
  if (notifsBtn && notifsDd) {{
    // У .topbar есть backdrop-filter: для position:fixed потомков он становится «окном» и
    // создаёт свой слой — список уезжал вправо и оказывался под карточками. Выносим его в <body>.
    document.body.appendChild(notifsDd);
    notifsDd.style.zIndex = '1000';
    fetch('/app/api/notifications', {{credentials:'same-origin'}})
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if (d.unread > 0) {{ notifsBadge.hidden = false; notifsBadge.textContent = d.unread > 9 ? '9+' : String(d.unread); }}
        renderNotifs(d.items || []);
      }})
      .catch(function(){{ notifsList.innerHTML = '<div style="padding:20px 16px;text-align:center;font:500 12px Manrope;color:var(--text-4);">Не удалось загрузить</div>'; }});
    function positionNotifsDd(){{
      var btnRect = notifsBtn.getBoundingClientRect();
      var ddWidth = Math.min(320, window.innerWidth - 24);
      var left = Math.min(btnRect.right - ddWidth, window.innerWidth - ddWidth - 12);
      left = Math.max(12, left);
      notifsDd.style.position = 'fixed';
      notifsDd.style.top = (btnRect.bottom + 10) + 'px';
      notifsDd.style.left = left + 'px';
      notifsDd.style.right = 'auto';
      notifsDd.style.width = ddWidth + 'px';
    }}
    notifsBtn.addEventListener('click', function(e){{
      e.stopPropagation();
      if (notifsDd.hidden) positionNotifsDd();
      notifsDd.hidden = !notifsDd.hidden;
      if (!notifsDd.hidden && !notifsBadge.hidden) {{
        // открыли колокольчик — отмечаем прочитанным на сервере, значок не вернётся после перезагрузки
        notifsBadge.hidden = true;
        fetch('/app/api/notifications/seen', {{method:'POST', credentials:'same-origin'}}).catch(function(){{}});
      }}
    }});
    document.querySelectorAll('[data-close-notifs]').forEach(function(b){{ b.addEventListener('click', function(){{ notifsDd.hidden = true; }}); }});
    document.addEventListener('click', function(e){{ if (!notifsDd.hidden && !notifsDd.contains(e.target) && !notifsBtn.contains(e.target)) notifsDd.hidden = true; }});
    window.addEventListener('scroll', function(e){{ if (!notifsDd.hidden && !(e.target instanceof Node && notifsDd.contains(e.target))) notifsDd.hidden = true; }}, {{passive: true, capture: true}});
    window.addEventListener('resize', function(){{ if (!notifsDd.hidden) notifsDd.hidden = true; }});
  }}
}})();
</script>"""


def site_footer() -> str:
    return f"""
<footer class="site-footer shell">
  <div style="display:flex;align-items:center;gap:10px;">
    <div style="width:22px;height:22px;border-radius:7px;background:linear-gradient(140deg,var(--accent),var(--accent-2));{_logo_box_style()}">{_logo_inner(22, 12) if site_logo_url() else ''}</div>
    <span style="font:700 13px Manrope;color:var(--text-3);">© 2025–2026 Flux Network</span>
  </div>
  <div class="footer-links">
    <a href="/legal/privacy">Политика конфиденциальности</a>
    <a href="/legal/terms">Пользовательское соглашение</a>
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


def ua_label(ua: str | None) -> str:
    """Короткая метка устройства/браузера из User-Agent, например «Windows · Chrome»."""
    ua = ua or ""
    if "iPhone" in ua or "iOS" in ua:
        plat = "iPhone"
    elif "Android" in ua:
        plat = "Android"
    elif "Macintosh" in ua:
        plat = "macOS"
    elif "Windows" in ua:
        plat = "Windows"
    elif "Linux" in ua:
        plat = "Linux"
    else:
        plat = "Устройство"
    if "Chrome" in ua:
        browser = "Chrome"
    elif "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua and "Chrome" not in ua:
        browser = "Safari"
    elif "Edg" in ua:
        browser = "Edge"
    else:
        browser = "браузер"
    return f"{plat} · {browser}"


def bot_deep_link(settings: Settings) -> str:
    uname = (settings.bot_username or "").strip().lstrip("@")
    return f"https://t.me/{uname}" if uname else "#"


def support_handle_link(settings: Settings) -> str:
    return bot_deep_link(settings)
