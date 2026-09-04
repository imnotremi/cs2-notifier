#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 Match Notifier -> Discord, 100% GitHub Actions.
Source : bo3.gg (gratuit, sans compte), via curl_cffi (empreinte TLS Chrome)
pour franchir le blocage anti-bot (403) depuis les IP GitHub.

Trois notifs :
  📢 Nouveau match d'une equipe suivie
  ⏰ Rappel quelques heures avant le coup d'envoi
  🏁 Score de fin de match
Etat anti-spam dans state.json (commite par le workflow).
"""

import json
import re
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    from curl_cffi import requests as cr
except ImportError:
    print("curl_cffi manquant : pip install curl_cffi")
    sys.exit(1)

# =====================================================================
#  CONFIG
# =====================================================================

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
# Ordre de PREFERENCE : Vitality d'abord, puis Spirit, puis 3DMAX.
# Quand deux equipes suivies se rencontrent, on parle toujours de la mieux classee ici.
TEAMS_TO_FOLLOW = ["Vitality", "Spirit", "3DMAX"]
REMINDER_HOURS_BEFORE = 4        # rappel si match dans <= X h (mets 10 pour "le matin meme")
RESULT_LOOKBACK_HOURS = 12       # notifie les scores des matchs finis dans les X dernieres h
LOCAL_TZ = "Europe/Paris"
DEBUG = os.environ.get("DEBUG", "") in ("1", "true", "True")

# Agenda : webhook Google Apps Script (ecrit dans l'agenda). Vide = desactive.
GCAL_WEBHOOK_URL = os.environ.get("GCAL_WEBHOOK_URL", "")
GCAL_WEBHOOK_TOKEN = os.environ.get("GCAL_WEBHOOK_TOKEN", "")

# =====================================================================
#  Moteur
# =====================================================================

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
BO3_URL = "https://api.bo3.gg/api/v1/matches"
BO3_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://bo3.gg",
    "referer": "https://bo3.gg/",
}
BASE_PARAMS = {
    "scope": "widget-matches",
    "page[offset]": "0",
    "page[limit]": "100",
    "filter[matches.discipline_id][eq]": "1",
    "with": "teams,tournament,games,stage,round",
}
COLOR_NEW = 0x89BFF4
COLOR_REMINDER = 0xFFB100
COLOR_RESULT = 0x3BA55D
COLOR_WIN = 0x3BA55D          # victoire de l'equipe suivie (vert)
COLOR_LOSS = 0xED4245         # defaite de l'equipe suivie (rouge)
COLOR_MOVED = 0xFF6B00        # match deplace / horaire change (orange vif)


def log(m):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


def load_state():
    empty = {"announced": [], "reminded": [], "scored": [], "calendared": [], "streamed": []}
    if not os.path.exists(STATE_FILE):
        return dict(empty)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in empty:
            d.setdefault(k, [])
        return d
    except Exception:
        return dict(empty)


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


LAST_RAW = {"status": None, "len": 0, "body": "", "url": ""}


def _fetch(status, sort, pages=1):
    from urllib.parse import urlencode
    out = []
    for pg in range(pages):
        p = dict(BASE_PARAMS)
        p["filter[matches.status][in]"] = status
        p["sort"] = sort
        p["page[offset]"] = str(pg * 100)
        # crochets/virgules litteraux : bo3 attend filter[...] non-encode
        url = BO3_URL + "?" + urlencode(p, safe="[],.")
        r = cr.get(url, headers=BO3_HEADERS, impersonate="chrome", timeout=30)
        body = r.text
        LAST_RAW.update({"status": r.status_code, "len": len(body), "body": body[:1200], "url": url})
        r.raise_for_status()
        data = r.json()
        chunk = (data.get("data") or data.get("results") or data.get("matches") or data.get("items") or []) \
            if isinstance(data, dict) else (data if isinstance(data, list) else [])
        out += chunk
        if len(chunk) < 100:
            break   # derniere page atteinte
    return out


def fetch_upcoming():
    # plusieurs pages -> couvre ~1 semaine de matchs (sinon 100 = ~1,5 jour, on ratait Vitality plus lointain)
    return _fetch("upcoming,current", "start_date", pages=6)


def fetch_finished():
    return _fetch("finished", "-start_date")


def first_present(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d.get(k)
    return None


def get_match_id(m):
    return first_present(m, ["id", "match_id", "slug"])


def get_slug(m):
    return first_present(m, ["slug", "url", "name"]) or ""


def bo3_url(m):
    """Page bo3.gg du match (elle affiche les streams officiels, Twitch inclus)."""
    slug = get_slug(m)
    return f"https://bo3.gg/matches/{slug}" if slug else None


# Chaines officielles ANGLAISES connues (le flag "official" de bo3 n'est pas fiable :
# EWC_Plus_EN y est marque official:false, alors que des casters regionaux sont official:true).
_OFFICIAL_HINTS = ("ewc_plus_en", "blastpremier", "esl_csgo", "eslcs", "pgl",
                   "dreamhack", "thescore", "gamersclub", "faceit", "iem", "blasttv")


def _pick_official_twitch_en(streams):
    """Meilleur stream TWITCH en ANGLAIS et officiel. None si aucun ne convient.
    On combine : chaine connue (+10) > suffixe _en (+5) > flag officiel bo3 (+2) > audience."""
    cand = [s for s in streams
            if "twitch.tv" in (s.get("raw_url") or "").lower()
            and (s.get("language") or "").lower() == "en"]
    if not cand:
        return None

    def score(s):
        name = (s.get("name") or "").lower()
        raw = (s.get("raw_url") or "").lower()
        sc = 0
        if any(h in name or h in raw for h in _OFFICIAL_HINTS):
            sc += 10
        if name.endswith("_en"):
            sc += 5
        if s.get("official"):
            sc += 2
        return (sc, s.get("viewers_number") or 0)

    best = max(cand, key=score)
    # si le meilleur n'a AUCUN signal officiel (que du community anglais) -> on n'envoie pas
    return best if score(best)[0] > 0 else None


def get_stream_url(m):
    """Lien du stream officiel ANGLAIS sur TWITCH (jamais Kick/YouTube), recupere en
    direct depuis bo3. None si pas dispo (match pas live / pas d'officiel anglais twitch)."""
    slug = get_slug(m)
    if not slug:
        return None
    try:
        r = cr.get(f"{BO3_URL}/{slug}", headers=BO3_HEADERS, impersonate="chrome", timeout=20)
        streams = (r.json() or {}).get("streams") or []
    except Exception:
        return None
    best = _pick_official_twitch_en(streams)
    return best.get("raw_url") if best else None


def get_team_names(m):
    names = []
    teams = m.get("teams")
    if isinstance(teams, list) and teams:
        for t in teams:
            if isinstance(t, dict):
                n = first_present(t, ["name", "clan_name", "title", "short_name", "acronym"])
                if n:
                    names.append(n)
    if len(names) < 2:
        for a, b in (("team1", "team2"), ("home", "away"), ("opponent1", "opponent2")):
            for k in (a, b):
                t = m.get(k)
                if isinstance(t, dict):
                    n = first_present(t, ["name", "clan_name", "title", "short_name"])
                    if n and n not in names:
                        names.append(n)
    if len(names) < 2:
        slug = get_slug(m).lower()
        if "-vs-" in slug:
            l, r = slug.split("-vs-", 1)
            x = l.replace("-", " ").strip().title()
            y = r.split("-20")[0].replace("-", " ").strip().title()
            names = [n for n in (x, y) if n][:2]
    while len(names) < 2:
        names.append("?")
    return names[0], names[1]


def _dt(raw):
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def get_start_utc(m):
    return _dt(first_present(m, ["start_date", "start_at", "begin_at", "scheduled_at", "date"]))


def get_end_utc(m):
    return _dt(first_present(m, ["end_date", "finished_at", "closed_at", "end_at"])) or get_start_utc(m)


def get_tournament(m):
    t = m.get("tournament") or m.get("tournament_deep") or {}
    if isinstance(t, dict):
        return first_present(t, ["name", "title", "full_name"]) or "Tournoi a confirmer"
    return "Tournoi a confirmer"


def get_format(m):
    v = first_present(m, ["bo_type", "number_of_games", "best_of", "bo"])
    return f"Bo{v}" if v else "?"


def get_score(m):
    """Retourne 's1 - s2' si trouvable, sinon None (parsing defensif)."""
    teams = m.get("teams")
    if isinstance(teams, list) and len(teams) >= 2:
        sc = [first_present(t, ["score", "wins", "result", "won_maps"]) for t in teams[:2]]
        if all(x is not None for x in sc):
            return f"{sc[0]} - {sc[1]}"
    for a, b in (("team1_score", "team2_score"), ("score_1", "score_2"),
                 ("home_score", "away_score"), ("results1", "results2")):
        s1, s2 = m.get(a), m.get(b)
        if s1 is not None and s2 is not None:
            return f"{s1} - {s2}"
    s = first_present(m, ["score", "final_score", "result"])
    if isinstance(s, str) and (":" in s or "-" in s):
        return s.replace(":", " - ")
    return None


def get_winner_name(m):
    wid = first_present(m, ["winner_team_id", "winner_clan_id", "winner_id"])
    if wid is None:
        return None
    teams = m.get("teams")
    if isinstance(teams, list):
        for t in teams:
            if isinstance(t, dict) and (t.get("id") == wid or t.get("team_id") == wid):
                return first_present(t, ["name", "clan_name", "title"])
    for k in ("team1", "team2"):
        t = m.get(k)
        if isinstance(t, dict) and (t.get("team_id") == wid or t.get("id") == wid):
            return first_present(t, ["name", "clan_name", "title"])
    return None


_STOP_WORDS = {"team", "esports", "gaming", "club", "the", "cs", "cs2"}


def _sig_words(name):
    # mots "significatifs" d'un nom d'equipe, hors mots generiques
    return {w for w in str(name).lower().replace(".", " ").replace("-", " ").split()
            if w and w not in _STOP_WORDS}


def followed_in(m):
    # match sur le NOM EXACT de l'equipe (ensemble de mots), pas une sous-chaine :
    # "Vitality" matche "Vitality"/"Team Vitality" mais PAS "Vitality Academy",
    # "Spirit" ne matche ni "Spirit Academy" ni "Spirit HU", etc.
    # On renvoie l'equipe la MIEUX classee dans TEAMS_TO_FOLLOW (ordre de preference),
    # donc un Vitality vs Spirit parle toujours de Vitality.
    names = [n for n in get_team_names(m) if n and n != "?"]
    name_sigs = [_sig_words(n) for n in names]
    for w in TEAMS_TO_FOLLOW:                 # parcours dans l'ordre de preference
        wsig = _sig_words(w)
        if wsig and any(wsig == s for s in name_sigs):
            return w
    return None


def _bo_number(m):
    v = first_present(m, ["bo_type", "number_of_games", "best_of", "bo"])
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _map_scores(m):
    """Score en MAPS gagnees (s1, s2) aligne sur team1/team2. (None, None) si illisible.
    bo3 fournit soit une liste `teams`, soit les champs `team1_score`/`team2_score`."""
    teams = m.get("teams")
    if isinstance(teams, list) and len(teams) >= 2:
        vals = [first_present(t, ["score", "wins", "won_maps", "result"]) for t in teams[:2]]
        try:
            return int(vals[0]), int(vals[1])
        except (TypeError, ValueError):
            pass
    for a, b in (("team1_score", "team2_score"), ("score_1", "score_2"),
                 ("home_score", "away_score"), ("results1", "results2")):
        s1, s2 = m.get(a), m.get(b)
        if s1 is not None and s2 is not None:
            try:
                return int(s1), int(s2)
            except (TypeError, ValueError):
                pass
    return None, None


def _match_complete(m):
    """True seulement si le match est VRAIMENT termine (le vainqueur a atteint le nombre
    de maps requis). Empeche d'annoncer un 'resultat' des la 1re map d'un Bo3."""
    bo = _bo_number(m)
    if not bo or bo < 2:
        return True                          # Bo1 (ou format inconnu) : 1 map suffit
    need = bo // 2 + 1                        # Bo3 -> 2 maps, Bo5 -> 3 maps
    s1, s2 = _map_scores(m)
    if s1 is None:
        return True                          # score illisible -> on ne bloque pas
    return max(s1, s2) >= need


def _ordered(m, followed):
    """(nom1, nom2, score1, score2) avec l'EQUIPE SUIVIE toujours en premier."""
    t1, t2 = get_team_names(m)
    s1, s2 = _map_scores(m)
    fsig = _sig_words(followed)
    if _sig_words(t2) == fsig and _sig_words(t1) != fsig:
        return t2, t1, s2, s1
    return t1, t2, s1, s2


def to_local(dt_utc):
    try:
        from zoneinfo import ZoneInfo
        loc = dt_utc.astimezone(ZoneInfo(LOCAL_TZ))
    except Exception:
        loc = dt_utc.astimezone(timezone(timedelta(hours=2)))
    j = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mo = ["", "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
          "aout", "septembre", "octobre", "novembre", "decembre"]
    return f"{j[loc.weekday()]} {loc.day} {mo[loc.month]} a {loc.strftime('%H:%M')}"


def send_embed(embed):
    data = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (compatible; CS2Notifier/1.0; +https://github.com/imnotremi/cs2-notifier)"},
                                 method="POST")
    urllib.request.urlopen(req, timeout=20)


def notify_match(title, followed, m, color, footer_extra="", stream_url=None):
    n1, n2 = _ordered(m, followed)[:2]       # equipe suivie en premier
    when = get_start_utc(m)
    fields = [
        {"name": "🗓️ Quand", "value": to_local(when) if when else "date a confirmer", "inline": False},
        {"name": "🏆 Tournoi", "value": get_tournament(m), "inline": True},
        {"name": "🎮 Format", "value": get_format(m), "inline": True},
    ]
    stream = stream_url or get_stream_url(m)
    url = stream or bo3_url(m)
    if url:
        name = "🔴 Stream officiel" if stream else "📺 Où regarder"
        label = "Regarder sur Twitch (officiel)" if stream else "Voir les streams (page bo3)"
        fields.append({"name": name, "value": f"[{label}]({url})", "inline": False})
    send_embed({
        "title": title,
        "description": f"**{n1}**  vs  **{n2}**",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Equipe suivie : {followed}{footer_extra}"},
    })

# ───────────────────────── phase du tournoi, tour, et "la suite" ─────────────────────────
_ROUND_FR = [
    (r"grand\s*final", "grande finale"), (r"semi[\s-]*final", "demi-finale"),
    (r"quarter[\s-]*final", "quart de finale"), (r"final", "finale"),
    (r"round\s*(\d+)", r"round \1"), (r"decider", "match décisif"),
]


def get_round(m):
    r = m.get("round") or {}
    return r if isinstance(r, dict) else {}


def get_stage_title(m):
    st = m.get("stage") or {}
    return (st.get("title") or "") if isinstance(st, dict) else ""


def round_fr(m):
    """'Upper bracket quarterfinal' -> 'quart de finale (upper bracket)' ; 'Grand final' -> 'grande finale'."""
    r = get_round(m)
    name = (r.get("name") or "").strip()
    if not name:
        return ""
    low = name.lower()
    core = re.sub(r"(upper|lower)\s*bracket\s*", "", low).strip()
    out = core
    for pat, fr in _ROUND_FR:
        if re.search(pat, core):
            out = re.sub(pat, fr, core)
            break
    bt = (r.get("bracket_type") or "").lower()
    if "upper" in low or bt == "upper":
        out += " (upper bracket)"
    elif "lower" in low or bt == "lower":
        out += " (lower bracket)"
    if r.get("is_decider") and "décisif" not in out:
        out += " — match décisif"
    return out


def _team_id_of(m, followed):
    for k in ("team1", "team2"):
        t = m.get(k) or {}
        if isinstance(t, dict) and t.get("name") and _sig_words(t["name"]) == _sig_words(followed):
            return t.get("id")
    return None


_TOURN_CACHE = {}


def fetch_tournament_matches(tid):
    """Tous les matchs d'un tournoi (finis + a venir), avec phase et tour. Cache par run."""
    if tid in _TOURN_CACHE:
        return _TOURN_CACHE[tid]
    from urllib.parse import urlencode
    out = []
    for status in ("finished", "upcoming,current"):
        for off in (0, 100, 200):
            p = {"filter[matches.tournament_id][eq]": str(tid), "filter[matches.status][in]": status,
                 "page[limit]": "100", "page[offset]": str(off), "with": "teams,tournament,stage,round",
                 "sort": "start_date"}
            r = cr.get(BO3_URL + "?" + urlencode(p, safe="[],."), headers=BO3_HEADERS, impersonate="chrome", timeout=30)
            if r.status_code != 200:
                break
            chunk = (r.json() or {}).get("results") or []
            out += chunk
            if len(chunk) < 100:
                break
    _TOURN_CACHE[tid] = out
    return out


def enrich_round(m):
    """La liste 'widget' de bo3 ne renvoie pas le tour : on le recupere dans la liste complete du tournoi."""
    if m.get("round"):
        return m
    tid = (m.get("tournament") or {}).get("id") or m.get("tournament_id")
    mid = get_match_id(m)
    if not tid or mid is None:
        return m
    try:
        for x in fetch_tournament_matches(tid):
            if get_match_id(x) == mid:
                for k in ("round", "stage", "stage_id", "round_id"):
                    if x.get(k) is not None:
                        m[k] = x[k]
                break
    except Exception as e:
        log(f"Tour du match non recupere ({e})")
    return m


def _article(rnd):
    return ("le " if rnd.startswith("round") else "la ") + rnd


def _fmt_next(nm_, followed):
    """'samedi 17:30 vs MOUZ' pour le prochain match (adversaire 'à déterminer' si inconnu)."""
    t1, t2 = get_team_names(nm_)
    opp = t2 if _sig_words(t1) == _sig_words(followed) else t1
    if not opp or re.fullmatch(r"[0-9a-f]{8,}", opp or "") or opp.lower() in ("none", "tbd"):
        opp = "adversaire à déterminer"
    w = get_start_utc(nm_)
    return f"{to_local(w) if w else 'date à confirmer'} vs {opp}"


def aftermath(followed, m, won):
    """Ce qui attend l'equipe suivie apres ce match : qualifiee (prochain match), eliminee, lower bracket, championne.
    Renvoie (texte, est_elimine, est_champion)."""
    tid = (m.get("tournament") or {}).get("id") or m.get("tournament_id")
    mid = get_match_id(m)
    team_id = _team_id_of(m, followed)
    rname = (get_round(m).get("name") or "").lower()
    bt = (get_round(m).get("bracket_type") or "").lower()
    stage_id = m.get("stage_id") or (m.get("stage") or {}).get("id")
    stage_title = get_stage_title(m).lower()
    is_final = bool(re.search(r"final", rname)) and not re.search(r"semi|quarter", rname)
    if not tid or not team_id:
        return ("", False, False)
    try:
        allm = fetch_tournament_matches(tid)
    except Exception as e:
        log(f"Suite tournoi indisponible ({e})")
        return ("", False, False)
    # le prochain match de l'equipe dans CE tournoi (pas encore joue)
    nxt = [x for x in allm if get_match_id(x) != mid and str(x.get("status")) in ("upcoming", "current")
           and team_id in (x.get("team1_id"), x.get("team2_id"))]
    nxt.sort(key=lambda x: str(x.get("start_date") or ""))
    # la phase a-t-elle un lower bracket ? (double elimination -> perdre en upper n'elimine pas)
    lower_exists = any((x.get("round") or {}).get("bracket_type") == "lower" for x in allm
                       if (x.get("stage_id") or (x.get("stage") or {}).get("id")) == stage_id)
    group_like = any(k in stage_title for k in ("group", "swiss", "groupe")) and not lower_exists and not re.search(r"final", rname)
    if won:
        if nxt:
            nr = round_fr(nxt[0])
            return (f"✅ Qualifié pour {_article(nr) if nr else 'la suite'} — prochain match : {_fmt_next(nxt[0], followed)}", False, False)
        if is_final and bt != "lower":
            return ("🏆 **CHAMPION DU TOURNOI !**", False, True)
        if is_final and bt == "lower":
            return ("✅ Qualifié pour la grande finale — adversaire et horaire à confirmer", False, False)
        return ("✅ Qualifié — prochain match pas encore programmé", False, False)
    # defaite
    if nxt:
        where = " (lower bracket)" if (get_round(nxt[0]).get("bracket_type") or "").lower() == "lower" else ""
        return (f"↘️ Toujours en course{where} — prochain match : {_fmt_next(nxt[0], followed)}", False, False)
    if bt == "upper" and lower_exists:
        return ("↘️ Descend en lower bracket — prochain match pas encore programmé", False, False)
    if group_like and not get_round(m).get("is_decider"):
        return ("Phase de groupes — prochain match pas encore programmé", False, False)
    where = f" en {round_fr(m)}" if round_fr(m) else ""
    return (f"❌ **ÉLIMINÉ du tournoi**{where}", True, False)


def notify_result(followed, m):
    # tout est raconte du point de vue de l'equipe suivie (elle est en premier)
    m = enrich_round(m)
    n1, n2, s1, s2 = _ordered(m, followed)
    winner = get_winner_name(m)
    won = bool(winner) and _sig_words(winner) == _sig_words(followed)
    if s1 is not None and s2 is not None:
        desc = f"**{n1}**  {s1} - {s2}  **{n2}**"
    else:
        desc = f"**{n1}**  vs  **{n2}**  (termine)"
    rnd = round_fr(m)
    tourn = get_tournament(m) + (f" • {rnd}" if rnd else "")
    fields = [{"name": "🏆 Tournoi", "value": tourn, "inline": True}]
    title = f"🏁 Resultat — {followed}"
    if winner:
        verdict = f"✅ Victoire de {followed} !" if won else f"❌ Defaite de {followed}"
        fields.append({"name": "Resultat", "value": verdict, "inline": True})
        color = COLOR_WIN if won else COLOR_LOSS
        try:
            suite, elim, champ = aftermath(followed, m, won)
        except Exception as e:
            log(f"Suite du tournoi non calculee : {e}"); suite, elim, champ = "", False, False
        if suite:
            fields.append({"name": "Et maintenant ?", "value": suite, "inline": False})
        if champ:
            title = f"🏆 CHAMPION — {followed}"
        elif elim:
            title = f"❌ ÉLIMINÉ — {followed}" + (f" ({rnd})" if rnd else "")
        elif rnd:
            title = f"🏁 Resultat — {followed} ({rnd})"
    else:
        color = COLOR_RESULT
    send_embed({
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": f"Equipe suivie : {followed}"},
    })


def notify_moved(followed, m, old_iso, new_dt):
    """Notif SPECIALE quand l'horaire d'un match a change (ex : 18h -> 13h)."""
    m = enrich_round(m)
    n1, n2 = _ordered(m, followed)[:2]
    old_dt = _dt(old_iso)
    # avance ou retarde, et de combien (ex : "avancé de 2 h 30")
    sens, duree = "déplacé", ""
    if old_dt:
        delta = (new_dt - old_dt).total_seconds()
        mins = int(round(abs(delta) / 60))
        h, mn = divmod(mins, 60)
        duree = (f"{h} h" if h else "") + (f" {mn:02d}" if (h and mn) else (f"{mn} min" if (not h) else ""))
        duree = duree.strip()
        sens = "avancé" if delta < 0 else "retardé"
    emoji = "⏩" if sens == "avancé" else ("⏳" if sens == "retardé" else "⏰")
    fields = [
        {"name": "🕐 Ancien horaire", "value": to_local(old_dt) if old_dt else "?", "inline": True},
        {"name": "🆕 Nouvel horaire", "value": to_local(new_dt), "inline": True},
        {"name": "🏆 Tournoi", "value": get_tournament(m) + (f" • {round_fr(m)}" if round_fr(m) else ""), "inline": False},
    ]
    send_embed({
        "title": f"{emoji} MATCH {sens.upper()}" + (f" DE {duree.upper()}" if duree else "") + f" — {followed}",
        "description": f"**{n1}**  vs  **{n2}**\n{emoji} Le match est {sens}" + (f" de **{duree}**" if duree else "") + f" : il commence à **{to_local(new_dt)}**.",
        "color": COLOR_MOVED,
        "fields": fields,
        "footer": {"text": f"Equipe suivie : {followed}  •  agenda mis a jour"},
    })


def _match_duration_minutes(m):
    v = first_present(m, ["bo_type", "number_of_games", "best_of", "bo"])
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 3
    return {1: 75, 2: 120, 3: 180, 5: 300}.get(n, 150)


def add_to_calendar(m, followed, mid):
    """Cree OU met a jour l'evenement dans l'agenda via le webhook Google Apps
    Script. Le matchId permet a l'Apps Script de retrouver le bon event et de
    le mettre a jour (au lieu de creer un doublon) quand l'heure change."""
    if not GCAL_WEBHOOK_URL:
        return False
    start = get_start_utc(m)
    if not start:
        return False
    t1, t2 = get_team_names(m)
    payload = {
        "token": GCAL_WEBHOOK_TOKEN,
        "matchId": str(mid),
        "title": f"CS2 — {t1} vs {t2}",
        "start": start.isoformat(),
        "durationMinutes": _match_duration_minutes(m),
        "description": f"{get_tournament(m)} • {get_format(m)} • equipe suivie : {followed}",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GCAL_WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    urllib.request.urlopen(req, timeout=20)
    return True


def main():
    if not DISCORD_WEBHOOK_URL:
        log("Secret DISCORD_WEBHOOK_URL absent. Ajoute-le dans Settings > Secrets > Actions.")
        sys.exit(1)

    try:
        upcoming = fetch_upcoming()
        finished = fetch_finished()
    except Exception as e:
        log(f"Echec bo3.gg : {e}")
        sys.exit(1)

    log(f"{len(upcoming)} a venir / {len(finished)} termines recuperes.")

    if DEBUG:
        log("=== DEBUG : aucune notif envoyee ===")
        log(f"[NET] derniere requete bo3 : status={LAST_RAW['status']} len={LAST_RAW['len']}")
        log(f"[NET] url={LAST_RAW['url']}")
        log(f"[NET] corps[:1200]={LAST_RAW['body']}")
        for label, lst in (("A VENIR", upcoming), ("TERMINES", finished)):
            log(f"-- {label} pour tes equipes --")
            for m in lst:
                f = followed_in(m)
                if f:
                    t1, t2 = get_team_names(m)
                    w = get_start_utc(m)
                    extra = f" | score={get_score(m)}" if lst is finished else ""
                    log(f"  [{f}] {t1} vs {t2} | {to_local(w) if w else '?'} | {get_tournament(m)}{extra}")
        if upcoming:
            log("Structure brute 1er match A VENIR :")
            print(json.dumps(upcoming[0], indent=2, ensure_ascii=False)[:1500])
        if finished:
            log("Structure brute 1er match TERMINE (pour caler le score) :")
            print(json.dumps(finished[0], indent=2, ensure_ascii=False)[:1500])
        return

    state = load_state()
    now = datetime.now(timezone.utc)
    announced, reminded, scored = set(state["announced"]), set(state["reminded"]), set(state["scored"])
    _raw_cal = state.get("calendared") or []
    # nouveau format = dict {matchId: heure_iso} ; ancien format = liste -> on convertit
    calendared = dict(_raw_cal) if isinstance(_raw_cal, dict) else {str(x): None for x in _raw_cal}
    streamed = set(state.get("streamed") or [])
    seen_up, seen_fin = set(), set()

    for m in upcoming:
        mid = get_match_id(m)
        f = followed_in(m)
        if mid is None or not f:
            continue
        seen_up.add(mid)
        when = get_start_utc(m)
        smid = str(mid)
        cur_start = when.isoformat() if when else None
        # nouveau match OU heure changee (bo3 a corrige) -> on (re)pousse dans l'agenda
        if when and calendared.get(smid) != cur_start:
            old_iso = calendared.get(smid)   # None = 1re fois ; sinon = ancien horaire
            try:
                if add_to_calendar(m, f, mid):
                    calendared[smid] = cur_start
                    log(f"Agenda maj {mid} ({f}) -> {to_local(when)}")
                    # deja programme a une AUTRE heure (>= 5 min d'ecart) -> MATCH DEPLACE
                    old_dt = _dt(old_iso) if old_iso else None
                    if old_dt and abs((when - old_dt).total_seconds()) >= 300:
                        try:
                            notify_moved(f, m, old_iso, when)
                            log(f"Deplacement notifie {mid} ({f}) : {to_local(old_dt)} -> {to_local(when)}")
                        except Exception as e:
                            log(f"Echec notif deplacement {mid}: {e}")
            except Exception as e:
                log(f"Echec agenda {mid}: {e}")
        if mid not in announced:
            try:
                notify_match(f"📢 Nouveau match — {f}", f, m, COLOR_NEW)
                announced.add(mid); log(f"Annonce {mid} ({f})")
            except Exception as e:
                log(f"Echec annonce {mid}: {e}")
        if when and mid not in reminded:
            delta = when - now
            if timedelta(0) <= delta <= timedelta(hours=REMINDER_HOURS_BEFORE):
                h = max(0, int(delta.total_seconds() // 3600)); mn = int((delta.total_seconds() % 3600) // 60)
                try:
                    notify_match(f"⏰ Ca joue bientot — {f}", f, m, COLOR_REMINDER,
                                 footer_extra=f"  •  dans ~{h}h{mn:02d}")
                    reminded.add(mid); log(f"Rappel {mid} ({f})")
                except Exception as e:
                    log(f"Echec rappel {mid}: {e}")
        # match EN DIRECT -> notif avec le stream OFFICIEL (envoyee une seule fois,
        # et SEULEMENT quand l'officiel est dispo -> jamais un streamer random)
        if m.get("status") == "current" and mid not in streamed:
            su = get_stream_url(m)
            if su:
                try:
                    notify_match(f"🔴 En direct — {f}", f, m, COLOR_REMINDER, stream_url=su)
                    streamed.add(mid); log(f"Live {mid} ({f})")
                except Exception as e:
                    log(f"Echec live {mid}: {e}")

    for m in finished:
        mid = get_match_id(m)
        f = followed_in(m)
        if mid is None or not f:
            continue
        seen_fin.add(mid)
        end = get_end_utc(m)
        if mid not in scored and end and (now - end) <= timedelta(hours=RESULT_LOOKBACK_HOURS):
            if not _match_complete(m):
                log(f"Resultat {mid} ({f}) ignore : match pas fini (ex : 1re map d'un Bo3)")
                continue
            try:
                notify_result(f, m); scored.add(mid); log(f"Resultat {mid} ({f})")
            except Exception as e:
                log(f"Echec resultat {mid}: {e}")

    state["announced"] = [x for x in announced if x in seen_up]
    state["reminded"] = [x for x in reminded if x in seen_up]
    state["scored"] = [x for x in scored if x in seen_fin]
    _seen_str = {str(x) for x in seen_up}
    state["calendared"] = {k: v for k, v in calendared.items() if k in _seen_str}
    state["streamed"] = [x for x in streamed if x in seen_up]
    save_state(state)
    log("Termine.")


if __name__ == "__main__":
    main()
