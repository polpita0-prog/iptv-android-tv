# -*- coding: utf-8 -*-
"""
IPTV Client PRO - Edycja Android TV & Windows (Flet)
Obsługa pilota TV (D-Pad), wbudowany odtwarzacz wideo + integracja z VLC, TMDB PL i EPG.
"""

import os
import re
import sys
import json
import base64
import datetime
import subprocess
import threading
from urllib.parse import urlparse, parse_qs

import requests
import flet as ft

# Próba załadowania natywnego modułu wideo Flet (działa na Android TV i Windows)
try:
    import flet_video as ftv
    HAS_FLET_VIDEO = True
except ImportError:
    HAS_FLET_VIDEO = False

# ==========================================
# KONFIGURACJA ZAPASOWEGO API (TMDB)
# ==========================================
TMDB_API_KEY = "6e2d327ee657c8671b3eda952cd260a0"

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "*/*",
}

SORT_OPTIONS = [
    "Od najnowszych (ostatnio wgrane)",
    "Alfabetycznie (A-Z)",
    "Alfabetycznie (Z-A)",
    "Domyślnie (kolejność serwera)",
]


# ==========================================
# FUNKCJE POMOCNICZE (Z TWOJEGO KODU)
# ==========================================
def make_session():
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=2, backoff_factor=1.0,
                      status_forcelist=[502, 503, 504],
                      allowed_methods=["GET"])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
    except Exception:
        pass
    return s


def extract_xtream_from_m3u_url(m3u_url):
    try:
        parsed = urlparse(m3u_url.strip())
        if not parsed.scheme or not parsed.netloc:
            return None
        qs = parse_qs(parsed.query)
        username = (qs.get("username") or qs.get("user") or [None])[0]
        password = (qs.get("password") or qs.get("pass") or [None])[0]
        if username and password:
            server = f"{parsed.scheme}://{parsed.netloc}"
            return server, username, password
    except Exception:
        pass
    return None


def _safe_int(val):
    if val is None:
        return 0
    try:
        return int(float(str(val).strip()))
    except Exception:
        m = re.findall(r'\d+', str(val))
        return int(m[-1]) if m else 0


def get_added_sort_key(item, kind):
    if kind == "vod":
        ts = _safe_int(item.get("added") or item.get("created_at") or item.get("releaseDate"))
        sid = _safe_int(item.get("stream_id") or item.get("num"))
        return (ts, sid)
    elif kind == "series":
        ts = _safe_int(item.get("last_modified") or item.get("added"))
        sid = _safe_int(item.get("series_id") or item.get("num"))
        return (ts, sid)
    else:
        sid = _safe_int(item.get("stream_id") or item.get("num"))
        return (0, sid)


def format_added_date(item, kind):
    raw_ts = item.get("added") if kind == "vod" else item.get("last_modified")
    ts = _safe_int(raw_ts)
    if ts > 1000000000:
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return "Brak danych"


def clean_media_title(title):
    if not title:
        return ""
    clean = re.sub(r'^(\|?[A-Za-z0-9]{2,5}\|?\s*[:|\-]+\s*)+', '', title)
    clean = re.sub(r'\[.*?\]', '', clean)
    clean = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d{1,3}.*$', '', clean)
    clean = re.sub(r'\s*\(\d{4}\).*$', '', clean)
    clean = re.sub(r'\b(FHD|UHD|4K|HD|1080p|720p|HEVC|H265|LEKTOR|DUBBING|NAPISY)\b.*$', '', clean, flags=re.IGNORECASE)
    return clean.strip(" -|_:")


def fetch_tmdb_details(title):
    if not TMDB_API_KEY:
        return None
    clean_title = clean_media_title(title)
    if not clean_title:
        return None
    try:
        url = (f"https://api.themoviedb.org/3/search/multi"
               f"?api_key={TMDB_API_KEY}&query={requests.utils.quote(clean_title)}&language=pl-PL")
        r = requests.get(url, timeout=4)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                best = results[0]
                poster_path = best.get("poster_path")
                release = best.get("release_date") or best.get("first_air_date") or ""
                return {
                    "poster": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                    "plot": best.get("overview") or "",
                    "rating": str(round(best.get("vote_average", 0), 1)) if best.get("vote_average") else "",
                    "year": release[:4] if release else "",
                }
    except Exception:
        pass
    return None


def decode_epg_text(text):
    if not text:
        return ""
    try:
        decoded = base64.b64decode(text).decode("utf-8")
        if all(ord(c) >= 32 or c in "\n\r\t" for c in decoded):
            return decoded
    except Exception:
        pass
    return text


# ==========================================
# KLIENT XTREAM CODES API & M3U
# ==========================================
class XtreamClient:
    def __init__(self, server, username, password):
        self.server = server.rstrip("/")
        self.username = username
        self.password = password
        self.session = make_session()
        self.user_info = None
        self.server_info = None

    def _api(self, params, timeout=15):
        url = f"{self.server}/player_api.php"
        full_params = {"username": self.username, "password": self.password}
        full_params.update(params)
        try:
            r = self.session.get(url, params=full_params, timeout=timeout)
        except requests.exceptions.Timeout:
            raise ValueError("Serwer zbyt długo nie odpowiada (przekroczono limit czasu).")
        except requests.exceptions.ConnectionError as e:
            raise ValueError(f"Błąd połączenia z serwerem: {e}")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise ValueError("Serwer odpowiedział, ale nie w formacie JSON.")

    def login(self):
        data = self._api({})
        self.user_info = data.get("user_info", {})
        self.server_info = data.get("server_info", {})
        if (self.user_info or {}).get("auth") != 1:
            raise ValueError("Nieprawidłowy login/hasło albo serwer odrzucił połączenie.")
        return self.user_info, self.server_info

    def get_categories(self, kind):
        action = {
            "live": "get_live_categories",
            "vod": "get_vod_categories",
            "series": "get_series_categories",
        }[kind]
        return self._api({"action": action}) or []

    def get_streams(self, kind, category_id=None):
        action = {
            "live": "get_live_streams",
            "vod": "get_vod_streams",
            "series": "get_series",
        }[kind]
        params = {"action": action}
        if category_id and category_id != "__ALL__":
            params["category_id"] = category_id
        return self._api(params, timeout=25) or []

    def get_vod_info(self, vod_id):
        return self._api({"action": "get_vod_info", "vod_id": vod_id})

    def get_series_info(self, series_id):
        return self._api({"action": "get_series_info", "series_id": series_id})

    def get_short_epg(self, stream_id, limit=6):
        return self._api({"action": "get_short_epg", "stream_id": stream_id, "limit": limit}, timeout=5)

    def build_stream_url(self, kind, stream_id, container_ext="ts", item=None):
        if item and item.get("direct_url"):
            return item["direct_url"]
        if kind == "live":
            return f"{self.server}/live/{self.username}/{self.password}/{stream_id}.{container_ext}"
        if kind == "vod":
            return f"{self.server}/movie/{self.username}/{self.password}/{stream_id}.{container_ext}"
        if kind == "series":
            return f"{self.server}/series/{self.username}/{self.password}/{stream_id}.{container_ext}"
        raise ValueError(kind)


class M3UClient:
    def __init__(self, source):
        self.source = source.strip()
        self.username = "Lista M3U"
        self.session = make_session()
        self.categories = {"live": [], "vod": [], "series": []}
        self.streams_by_cat = {"live": {}, "vod": {}, "series": {}}
        self.vod_map = {}
        self.series_map = {}
        self.direct_urls = {}

    def login(self):
        content = self._fetch_content()
        self._parse_m3u(content)
        total = (
            sum(len(v) for v in self.streams_by_cat["live"].values()) +
            sum(len(v) for v in self.streams_by_cat["vod"].values()) +
            sum(len(v) for v in self.streams_by_cat["series"].values())
        )
        if total == 0:
            raise ValueError("Nie znaleziono żadnych kanałów ani filmów w podanej liście M3U.")
        return {"auth": 1, "exp_date": "0"}, {}

    def _fetch_content(self):
        if os.path.exists(self.source):
            with open(self.source, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        if self.source.lower().startswith(("http://", "https://")):
            r = self.session.get(self.source, timeout=25)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        raise ValueError("Podaj prawidłowy link (http/https) do listy M3U.")

    def _classify_entry(self, url, group, name):
        lower_url = url.lower()
        lower_group = group.lower()
        ep_match = re.search(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})', name)
        if "/series/" in lower_url or ep_match or any(k in lower_group for k in ["seriale", "series", "serial"]):
            return "series", ep_match
        if (
            "/movie/" in lower_url
            or lower_url.endswith((".mp4", ".mkv", ".avi"))
            or any(k in lower_group for k in ["vod", "filmy", "movies", "film", "kino", "premiery"])
        ):
            return "vod", None
        return "live", None

    def _parse_m3u(self, text):
        cat_sets = {"live": {}, "vod": {}, "series": {}}
        series_groups = {}
        attr_re = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
        extinf_line = None
        seq_id = 1

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                extinf_line = line
                continue
            if line.startswith("#"):
                continue

            url = line
            attrs = dict(attr_re.findall(extinf_line)) if extinf_line else {}
            name = ""
            if extinf_line and "," in extinf_line:
                name = extinf_line.split(",", 1)[1].strip()
            if not name:
                name = attrs.get("tvg-name") or f"Pozycja {seq_id}"

            group = (attrs.get("group-title") or "Bez kategorii").strip()
            logo = attrs.get("tvg-logo") or ""
            kind, ep_match = self._classify_entry(url, group, name)

            if group not in cat_sets[kind]:
                cat_id = f"m3u_{kind}_cat_{len(cat_sets[kind]) + 1}"
                cat_sets[kind][group] = cat_id
                self.categories[kind].append({"category_id": cat_id, "category_name": group})
                self.streams_by_cat[kind][cat_id] = []
            else:
                cat_id = cat_sets[kind][group]

            current_num = seq_id
            item_id = f"m3u_item_{current_num}"
            seq_id += 1
            self.direct_urls[item_id] = url

            if kind == "live":
                self.streams_by_cat["live"][cat_id].append({
                    "stream_id": item_id, "num": current_num, "name": name,
                    "stream_icon": logo, "direct_url": url,
                })
            elif kind == "vod":
                item_obj = {
                    "stream_id": item_id, "num": current_num, "added": current_num,
                    "name": name, "stream_icon": logo, "direct_url": url,
                }
                self.streams_by_cat["vod"][cat_id].append(item_obj)
                self.vod_map[item_id] = item_obj
            elif kind == "series":
                if ep_match:
                    season_num = str(int(ep_match.group(1)))
                    ep_num = int(ep_match.group(2))
                    base_title = name[:ep_match.start()].strip(" -|:") or name
                else:
                    season_num = "1"
                    ep_num = 1
                    base_title = name

                series_key = (cat_id, base_title.lower())
                if series_key not in series_groups:
                    series_id = f"m3u_ser_{current_num}"
                    series_groups[series_key] = series_id
                    series_entry = {
                        "series_id": series_id, "num": current_num,
                        "last_modified": current_num, "name": base_title, "cover": logo,
                    }
                    self.streams_by_cat["series"][cat_id].append(series_entry)
                    self.series_map[series_id] = {
                        "info": {"name": base_title, "cover": logo, "plot": "", "genre": group},
                        "episodes": {},
                        "entry_ref": series_entry,
                    }
                else:
                    series_id = series_groups[series_key]
                    self.series_map[series_id]["entry_ref"]["last_modified"] = current_num

                eps_dict = self.series_map[series_id]["episodes"]
                eps_list = eps_dict.setdefault(season_num, [])
                if not ep_match:
                    ep_num = len(eps_list) + 1
                eps_list.append({
                    "id": item_id, "episode_num": ep_num, "title": name, "direct_url": url,
                })
            extinf_line = None

    def get_categories(self, kind):
        return self.categories.get(kind, [])

    def get_streams(self, kind, category_id=None):
        cat_map = self.streams_by_cat.get(kind, {})
        if not category_id or category_id == "__ALL__":
            all_items = []
            for items in cat_map.values():
                all_items.extend(items)
            return all_items
        return cat_map.get(category_id, [])

    def get_vod_info(self, vod_id):
        item = self.vod_map.get(vod_id, {})
        return {"info": {"cover_big": item.get("stream_icon", ""), "plot": "", "genre": "M3U VOD"}, "movie_data": {}}

    def get_series_info(self, series_id):
        return self.series_map.get(series_id, {"info": {}, "episodes": {}})

    def get_short_epg(self, stream_id, limit=6):
        return {"epg_listings": []}

    def build_stream_url(self, kind, stream_id, container_ext="ts", item=None):
        if item and item.get("direct_url"):
            return item["direct_url"]
        return self.direct_urls.get(stream_id, "")


# ==========================================
# GŁÓWNY INTERFEJS TELEWIZYJNY (FLET - ANDROID TV / WINDOWS)
# ==========================================
def main(page: ft.Page):
    page.title = "IPTV Client PRO (Android TV & PC)"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#13131c"
    page.padding = 0

    # Ścieżka zapisu danych bezpieczna dla Androida i Windowsa
    storage_dir = os.path.expanduser("~")
    config_file = os.path.join(storage_dir, "iptv_tv_config.json")
    fav_file = os.path.join(storage_dir, "iptv_tv_favorites.json")

    state = {
        "client": None,
        "kind": "live",
        "categories": [],
        "all_streams": [],
        "streams": [],
        "selected_item": None,
        "current_url": None,
        "current_title": "",
        "tmdb_cache": {},
    }

    def load_json(path, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def save_json(path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    config = load_json(config_file, {})
    favorites = load_json(fav_file, {"live": [], "vod": [], "series": []})

    def get_item_id(item):
        return item.get("series_id") if state["kind"] == "series" else item.get("stream_id")

    def is_fav(item):
        iid = get_item_id(item)
        return any(get_item_id(f) == iid for f in favorites.get(state["kind"], [])) if iid else False

    def show_snack(msg, color="#4da3ff"):
        page.snack_bar = ft.SnackBar(ft.Text(msg, color="white"), bgcolor=color)
        page.snack_bar.open = True
        page.update()

    # ------------------------------------------
    # ODTWARZANIE: WBUDOWANY PLAYER LUB VLC
    # ------------------------------------------
    def launch_external_vlc(url):
        if sys.platform.startswith("win"):
            vlc_paths = [
                r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            ]
            for p in vlc_paths:
                if os.path.exists(p):
                    subprocess.Popen([p, "--network-caching=800", url])
                    return
            subprocess.Popen(f'start vlc "{url}"', shell=True)
        else:
            # Na Android TV wywołanie vlc:// otwiera aplikację VLC for Android
            vlc_intent_url = f"vlc://{url}"
            page.launch_url(vlc_intent_url)

    def open_builtin_player(url, title):
        state["current_url"] = url
        state["current_title"] = title

        if not HAS_FLET_VIDEO:
            show_snack("Brak modułu flet-video – przekierowuję do VLC...", "#ffab00")
            launch_external_vlc(url)
            return

        video_player = ftv.Video(
            expand=True,
            playlist=[ftv.VideoMedia(url)],
            playlist_mode=ftv.PlaylistMode.NONE,
            fill_color="black",
            aspect_ratio=16 / 9,
            volume=100,
            autoplay=True,
            filter_quality=ft.FilterQuality.HIGH,
        )

        def close_player(_e=None):
            page.overlay.clear()
            page.update()

        player_overlay = ft.Container(
            bgcolor="black",
            expand=True,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor="#1b1b2a",
                        padding=10,
                        content=ft.Row(
                            [
                                ft.ElevatedButton(
                                    "⬅ Wróć do listy (Pilot: Wstecz)",
                                    on_click=close_player,
                                    bgcolor="#ff5252",
                                    color="white",
                                    autofocus=True,
                                ),
                                ft.Text(title, size=18, weight=ft.FontWeight.BOLD, expand=True),
                                ft.ElevatedButton(
                                    "Otwórz w zewnętrznym VLC",
                                    on_click=lambda _: launch_external_vlc(url),
                                    bgcolor="#ff6d00",
                                    color="white",
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    ),
                    video_player,
                ],
                spacing=0,
                expand=True,
            ),
        )
        page.overlay.append(player_overlay)
        page.update()

    # ------------------------------------------
    # EKRAN LOGOWANIA
    # ------------------------------------------
    server_tf = ft.TextField(label="Adres serwera (np. http://serwer:8080)", value=config.get("server", "http://"), width=420)
    user_tf = ft.TextField(label="Login", value=config.get("username", ""), width=420)
    pass_tf = ft.TextField(label="Hasło", value=config.get("password", ""), password=True, can_reveal_password=True, width=420)
    m3u_tf = ft.TextField(label="LUB Wklej link M3U (opcjonalnie)", value=config.get("m3u_url", ""), width=420)
    login_status_txt = ft.Text("", color="#ff5252", size=14)
    login_btn = ft.ElevatedButton("POŁĄCZ Z SERWEREM", bgcolor="#4da3ff", color="white", width=420, height=50, autofocus=True)

    def extract_m3u_btn_click(_e):
        extracted = extract_xtream_from_m3u_url(m3u_tf.value)
        if extracted:
            server_tf.value, user_tf.value, pass_tf.value = extracted
            login_status_txt.value = "Wyciągnięto dane Xtream z linku M3U!"
            login_status_txt.color = "#00c853"
        else:
            login_status_txt.value = "Link nie zawiera loginu/hasła Xtream – użyj POŁĄCZ, by wczytać go jako zwykłe M3U."
            login_status_txt.color = "#ffab00"
        page.update()

    def do_login(_e):
        login_btn.disabled = True
        login_status_txt.value = "Łączenie z serwerem... Proszę czekać."
        login_status_txt.color = "#9090a0"
        page.update()

        def worker():
            try:
                if m3u_tf.value.strip() and (not user_tf.value.strip() or not pass_tf.value.strip()):
                    ext = extract_xtream_from_m3u_url(m3u_tf.value)
                    if ext:
                        client = XtreamClient(*ext)
                    else:
                        client = M3UClient(m3u_tf.value)
                else:
                    client = XtreamClient(server_tf.value.strip(), user_tf.value.strip(), pass_tf.value)

                u_info, _ = client.login()
                state["client"] = client
                save_json(config_file, {
                    "server": server_tf.value.strip(),
                    "username": user_tf.value.strip(),
                    "password": pass_tf.value,
                    "m3u_url": m3u_tf.value.strip(),
                })
                build_main_view(u_info)
            except Exception as ex:
                login_status_txt.value = f"Błąd logowania: {ex}"
                login_status_txt.color = "#ff5252"
                login_btn.disabled = False
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    login_btn.on_click = do_login

    login_view = ft.Container(
        expand=True,
        alignment=ft.alignment.center,
        content=ft.Container(
            bgcolor="#1b1b2a",
            padding=30,
            border_radius=12,
            width=480,
            content=ft.Column(
                [
                    ft.Text("IPTV Client PRO - TV", size=28, weight=ft.FontWeight.BOLD, color="#4da3ff"),
                    ft.Text("Nawiguj pilotem TV lub myszką", size=13, color="#9090a0"),
                    server_tf,
                    user_tf,
                    pass_tf,
                    ft.Divider(color="#2a2a3d"),
                    m3u_tf,
                    ft.TextButton("⚡ Wyciągnij dane Xtream z linku M3U", on_click=extract_m3u_btn_click),
                    login_btn,
                    login_status_txt,
                ],
                tight=True,
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ),
    )

    # ------------------------------------------
    # GŁÓWNY EKRAN TELEWIZYJNY (3 KOLUMNY)
    # ------------------------------------------
    cat_list_view = ft.ListView(expand=True, spacing=4, padding=6)
    stream_list_view = ft.ListView(expand=True, spacing=4, padding=6)

    search_tf = ft.TextField(
        hint_text="Szukaj...",
        height=42,
        content_padding=10,
        expand=True,
        bgcolor="#0d0d16",
    )
    sort_dd = ft.Dropdown(
        options=[ft.dropdown.Option(o) for o in SORT_OPTIONS],
        value=SORT_OPTIONS[3],
        width=250,
        height=42,
        content_padding=8,
        bgcolor="#0d0d16",
    )

    poster_img = ft.Image(src="", width=200, height=280, fit=ft.ImageFit.CONTAIN, visible=False)
    info_title_txt = ft.Text("Wybierz pozycję z listy", size=20, weight=ft.FontWeight.BOLD, color="#4da3ff")
    info_desc_txt = ft.Text("", size=14, color="#eaeaf0")
    status_counter_txt = ft.Text("", size=13, color="#9090a0")

    play_builtin_btn = ft.ElevatedButton(
        "▶ ODTWÓRZ W APLIKACJI",
        bgcolor="#00c853",
        color="white",
        height=46,
        disabled=True,
    )
    play_vlc_btn = ft.ElevatedButton(
        "📺 OTWÓRZ W VLC",
        bgcolor="#ff6d00",
        color="white",
        height=46,
        disabled=True,
    )
    fav_toggle_btn = ft.ElevatedButton(
        "☆ Ulubione",
        bgcolor="#252538",
        color="white",
        height=46,
        disabled=True,
    )

    def sort_streams(items):
        mode = sort_dd.value
        if mode == SORT_OPTIONS[0]:
            return sorted(items, key=lambda x: get_added_sort_key(x, state["kind"]), reverse=True)
        elif mode == SORT_OPTIONS[1]:
            return sorted(items, key=lambda x: (x.get("name") or x.get("title") or "").strip().lower())
        elif mode == SORT_OPTIONS[2]:
            return sorted(items, key=lambda x: (x.get("name") or x.get("title") or "").strip().lower(), reverse=True)
        return list(items)

    def render_streams():
        q = (search_tf.value or "").strip().lower()
        filtered = [
            s for s in state["all_streams"]
            if not q or q in (s.get("name") or s.get("title") or "").lower()
        ]
        state["streams"] = sort_streams(filtered)
        stream_list_view.controls.clear()

        # Limit wyświetlania naraz dla płynności na Android TV (250 pozycji)
        display_slice = state["streams"][:250]
        for item in display_slice:
            name = item.get("name") or item.get("title") or "???"
            fav_star = "★ " if is_fav(item) else ""
            btn = ft.TextButton(
                content=ft.Row([
                    ft.Text(f"{fav_star}{name}", color="#ffab00" if fav_star else "white", size=15, no_wrap=True, expand=True)
                ]),
                style=ft.ButtonStyle(
                    bgcolor={ft.ControlState.FOCUSED: "#4da3ff", ft.ControlState.HOVERED: "#252538"},
                    shape=ft.RoundedRectangleBorder(radius=6),
                ),
                on_click=lambda e, it=item: on_stream_clicked(it),
            )
            stream_list_view.controls.append(btn)

        status_counter_txt.value = f"Widoczne: {len(display_slice)} z {len(state['streams'])}"
        page.update()

    search_tf.on_change = lambda _: render_streams()
    sort_dd.on_change = lambda _: render_streams()

    def on_stream_clicked(item):
        state["selected_item"] = item
        title = item.get("name") or item.get("title") or ""
        info_title_txt.value = title
        info_desc_txt.value = "Pobieranie informacji / EPG..."
        poster_img.visible = False

        play_builtin_btn.disabled = False
        play_vlc_btn.disabled = False
        fav_toggle_btn.disabled = False
        fav_toggle_btn.text = "★ Usuń z ulubionych" if is_fav(item) else "☆ Dodaj do ulubionych"
        page.update()

        def details_worker():
            kind = state["kind"]
            client = state["client"]
            cover_url = item.get("stream_icon") or item.get("cover") or ""
            desc = ""

            if kind == "live":
                sid = item.get("stream_id")
                lines = []
                try:
                    epg = client.get_short_epg(sid, limit=5).get("epg_listings", [])
                    if epg:
                        lines.append("PROGRAM TV (EPG):\n")
                        for idx, entry in enumerate(epg):
                            pt = decode_epg_text(entry.get("title", ""))
                            pd = decode_epg_text(entry.get("description", ""))
                            st = entry.get("start", "")[11:16]
                            en = entry.get("end", "")[11:16]
                            pref = "▶ TERAZ: " if idx == 0 else "• "
                            lines.append(f"{pref}[{st}-{en}] {pt}")
                            if pd and idx == 0:
                                lines.append(f"   {pd}\n")
                    else:
                        lines.append("Brak programu EPG dla tego kanału.")
                except Exception:
                    lines.append("Brak danych EPG.")
                desc = "\n".join(lines)

            elif kind in ("vod", "series"):
                added = format_added_date(item, kind)
                try:
                    if kind == "vod":
                        data = client.get_vod_info(item.get("stream_id"))
                    else:
                        data = client.get_series_info(item.get("series_id"))
                    info = data.get("info", {}) if isinstance(data, dict) else {}
                    plot = (info.get("plot") or "").strip()
                    rating = str(info.get("rating") or "").strip()
                    genre = info.get("genre") or "Brak danych"
                    cover_url = info.get("cover_big") or info.get("cover") or cover_url

                    if not plot or not cover_url:
                        tmdb = fetch_tmdb_details(title)
                        if tmdb:
                            if not plot and tmdb.get("plot"):
                                plot = f"{tmdb['plot']} (TMDB PL)"
                            if not cover_url and tmdb.get("poster"):
                                cover_url = tmdb["poster"]
                            if not rating and tmdb.get("rating"):
                                rating = f"{tmdb['rating']}/10"

                    desc = f"Dodano: {added}\nGatunek: {genre} | Ocena: {rating or 'Brak'}\n\n{plot or 'Brak opisu.'}"
                except Exception:
                    desc = f"Dodano: {added}\nBrak szczegółowego opisu."

            if state["selected_item"] == item:
                info_desc_txt.value = desc
                if cover_url:
                    poster_img.src = cover_url
                    poster_img.visible = True
                page.update()

        threading.Thread(target=details_worker, daemon=True).start()

    def play_selected_item(use_vlc=False):
        item = state["selected_item"]
        if not item:
            return
        kind = state["kind"]
        client = state["client"]
        title = item.get("name") or item.get("title") or ""

        if kind in ("live", "vod"):
            sid = item.get("stream_id")
            default_ext = "ts" if kind == "live" else "mp4"
            ext = item.get("container_extension", default_ext) or default_ext
            url = client.build_stream_url(kind, sid, ext, item=item)
            if use_vlc:
                launch_external_vlc(url)
            else:
                open_builtin_player(url, title)
        elif kind == "series":
            open_series_episodes_modal(item, use_vlc)

    def open_series_episodes_modal(series_item, use_vlc):
        sid = series_item.get("series_id")
        title = series_item.get("name") or "Serial"
        show_snack("Pobieranie listy odcinków...")

        def worker():
            try:
                info = state["client"].get_series_info(sid)
                eps_by_season = info.get("episodes") or {}
                if not eps_by_season:
                    show_snack("Brak odcinków dla tego serialu.", "#ff5252")
                    return

                ep_controls = []
                for s_num in sorted(eps_by_season.keys(), key=lambda x: int(x) if str(x).isdigit() else 999):
                    ep_controls.append(ft.Text(f"Sezon {s_num}", size=18, weight=ft.FontWeight.BOLD, color="#4da3ff"))
                    for ep in eps_by_season[s_num]:
                        ep_title = ep.get("title") or f"Odcinek {ep.get('episode_num', '?')}"
                        ext = ep.get("container_extension", "mp4") or "mp4"
                        url = state["client"].build_stream_url("series", ep.get("id"), ext, item=ep)

                        def make_play_ep(u, t):
                            def _cb(_e):
                                page.close(dlg)
                                if use_vlc:
                                    launch_external_vlc(u)
                                else:
                                    open_builtin_player(u, t)
                            return _cb

                        ep_controls.append(
                            ft.TextButton(
                                text=f"▶ Odcinek {ep.get('episode_num', '?')}: {ep_title}",
                                on_click=make_play_ep(url, ep_title),
                            )
                        )

                dlg = ft.AlertDialog(
                    title=ft.Text(f"{title} - Wybierz odcinek"),
                    content=ft.Container(
                        width=500,
                        height=400,
                        content=ft.ListView(controls=ep_controls, spacing=6),
                    ),
                )
                page.open(dlg)
            except Exception as e:
                show_snack(f"Błąd ładowania odcinków: {e}", "#ff5252")

        threading.Thread(target=worker, daemon=True).start()

    play_builtin_btn.on_click = lambda _: play_selected_item(use_vlc=False)
    play_vlc_btn.on_click = lambda _: play_selected_item(use_vlc=True)

    def toggle_fav_click(_e):
        item = state["selected_item"]
        if not item:
            return
        iid = get_item_id(item)
        fav_list = favorites.setdefault(state["kind"], [])
        if is_fav(item):
            favorites[state["kind"]] = [f for f in fav_list if get_item_id(f) != iid]
        else:
            fav_list.append(item)
        save_json(fav_file, favorites)
        fav_toggle_btn.text = "★ Usuń z ulubionych" if is_fav(item) else "☆ Dodaj do ulubionych"
        render_streams()

    fav_toggle_btn.on_click = toggle_fav_click

    def load_categories_for_kind(kind):
        state["kind"] = kind
        sort_dd.value = SORT_OPTIONS[0] if kind in ("vod", "series") else SORT_OPTIONS[3]
        cat_list_view.controls.clear()
        stream_list_view.controls.clear()
        status_counter_txt.value = "Ładowanie kategorii..."
        page.update()

        def worker():
            try:
                cats = state["client"].get_categories(kind)
                fav_count = len(favorites.get(kind, []))
                special = [{"category_id": "__FAV__", "category_name": f"★ ULUBIONE ({fav_count})"}]
                if kind in ("vod", "series"):
                    special.append({"category_id": "__ALL__", "category_name": "🔥 OSTATNIO DODANE (WSZYSTKIE)"})
                state["categories"] = special + (cats or [])

                cat_list_view.controls.clear()
                for c in state["categories"]:
                    cid = c.get("category_id")
                    cname = c.get("category_name", "???")
                    col = "#ffab00" if cid == "__FAV__" else ("#ff6d00" if cid == "__ALL__" else "white")
                    btn = ft.TextButton(
                        content=ft.Row([ft.Text(cname, color=col, size=15, no_wrap=True, expand=True)]),
                        style=ft.ButtonStyle(
                            bgcolor={ft.ControlState.FOCUSED: "#4da3ff", ft.ControlState.HOVERED: "#252538"},
                            shape=ft.RoundedRectangleBorder(radius=6),
                        ),
                        on_click=lambda e, cat_id=cid: load_streams_for_cat(cat_id),
                    )
                    cat_list_view.controls.append(btn)
                status_counter_txt.value = f"Kategorie: {len(cats or [])}"
                page.update()
            except Exception as ex:
                show_snack(f"Błąd: {ex}", "#ff5252")

        threading.Thread(target=worker, daemon=True).start()

    def load_streams_for_cat(cat_id):
        stream_list_view.controls.clear()
        status_counter_txt.value = "Pobieranie listy pozycji..."
        page.update()

        if cat_id == "__FAV__":
            state["all_streams"] = list(favorites.get(state["kind"], []))
            render_streams()
            return

        def worker():
            try:
                streams = state["client"].get_streams(state["kind"], cat_id)
                state["all_streams"] = streams or []
                render_streams()
            except Exception as ex:
                show_snack(f"Błąd pobierania pozycji: {ex}", "#ff5252")

        threading.Thread(target=worker, daemon=True).start()

    def build_main_view(user_info):
        page.clean()
        top_bar = ft.Container(
            bgcolor="#1b1b2a",
            padding=12,
            content=ft.Row(
                [
                    ft.Text("IPTV PRO TV", size=20, weight=ft.FontWeight.BOLD, color="#4da3ff"),
                    ft.Row(
                        [
                            ft.ElevatedButton("📺 NA ŻYWO (LIVE)", on_click=lambda _: load_categories_for_kind("live"), bgcolor="#252538", color="white", autofocus=True),
                            ft.ElevatedButton("🎬 FILMY (VOD)", on_click=lambda _: load_categories_for_kind("vod"), bgcolor="#252538", color="white"),
                            ft.ElevatedButton("🍿 SERIALE", on_click=lambda _: load_categories_for_kind("series"), bgcolor="#252538", color="white"),
                        ],
                        spacing=10,
                    ),
                    status_counter_txt,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        )

        body = ft.Row(
            [
                # Kolumna 1: Kategorie
                ft.Container(
                    width=290,
                    bgcolor="#1b1b2a",
                    border_radius=8,
                    content=ft.Column([
                        ft.Container(padding=10, content=ft.Text("Kategorie", weight=ft.FontWeight.BOLD, color="#4da3ff")),
                        cat_list_view,
                    ]),
                ),
                # Kolumna 2: Kanały / Filmy
                ft.Container(
                    expand=True,
                    bgcolor="#1b1b2a",
                    border_radius=8,
                    padding=8,
                    content=ft.Column([
                        ft.Row([search_tf, sort_dd]),
                        stream_list_view,
                    ]),
                ),
                # Kolumna 3: Plakat, EPG / Opis z TMDB i przyciski Play
                ft.Container(
                    width=420,
                    bgcolor="#1b1b2a",
                    border_radius=8,
                    padding=16,
                    content=ft.Column(
                        [
                            ft.Row([poster_img], alignment=ft.MainAxisAlignment.CENTER),
                            info_title_txt,
                            ft.Row([play_builtin_btn, play_vlc_btn], wrap=True, spacing=8),
                            fav_toggle_btn,
                            ft.Divider(color="#2a2a3d"),
                            ft.Column([info_desc_txt], scroll=ft.ScrollMode.AUTO, expand=True),
                        ],
                        expand=True,
                    ),
                ),
            ],
            expand=True,
            spacing=10,
        )

        page.add(
            ft.Column(
                [top_bar, ft.Container(content=body, expand=True, padding=10)],
                expand=True,
                spacing=0,
            )
        )
        load_categories_for_kind("live")

    page.add(login_view)


if __name__ == "__main__":
    ft.app(target=main)
