from flask import Flask, jsonify, request, render_template
from pathlib import Path
import csv, json, os, re, secrets, threading, time
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
WORDS_FILE = BASE / 'words.csv'
STATE_FILE = BASE / 'state.json'
DICT_URL = 'https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt'
app = Flask(__name__)
lock = threading.RLock()
stop_event = threading.Event()
worker = None
state = {'running': False, 'phase': '待機中', 'total': 0, 'checked': 0, 'potential': [], 'log': [], 'last': '', 'words_loaded': 0, 'errors': 0, 'seen': {}}


def log(msg):
    with lock:
        state['last'] = msg
        state['log'].append(time.strftime('%H:%M:%S') + ' ' + msg)
        state['log'] = state['log'][-80:]


def load_state():
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding='utf-8'))
            state['potential'] = saved.get('potential', []) if isinstance(saved.get('potential', []), list) else []
            state['seen'] = saved.get('seen', {}) if isinstance(saved.get('seen', {}), dict) else {}
            state['checked'] = len(state['seen'])
        except Exception:
            pass


def save_state():
    with lock:
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'potential': state['potential'], 'seen': state['seen']}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(STATE_FILE)


def load_words():
    words = []
    try:
        with open(WORDS_FILE, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                w = (row.get('word') or '').strip().lower()
                if re.fullmatch(r'[a-z]{3,12}', w):
                    words.append(w)
    except Exception as e:
        log('内蔵辞書の読み込み失敗: ' + str(e))
    try:
        log('英単語辞書を取得中…')
        r = requests.get(DICT_URL, timeout=30, headers={'User-Agent': 'WordHunter/1.0'})
        r.raise_for_status()
        words.extend(x.strip().lower() for x in r.text.splitlines() if re.fullmatch(r'[a-z]{3,12}', x.strip().lower()))
        log('英単語辞書を取得しました')
    except Exception as e:
        log('大規模辞書の取得失敗。内蔵辞書で続行: ' + str(e))
    words = sorted(set(words))
    state['words_loaded'] = len(words)
    return words


def check_xboxgamertag_site(tag, sess):
    """Public directory lookup only. Absence is a hint, not proof of availability."""
    url = 'https://xboxgamertag.com/search/' + quote_plus(tag).replace('+', '%20')
    try:
        r = sess.get(url, timeout=18, allow_redirects=True)
        if r.status_code in (403, 429) or r.status_code >= 500:
            return 'unknown', 'XboxGamertag.com blocked/limited HTTP ' + str(r.status_code)
        if r.status_code == 404:
            return 'not_found', 'XboxGamertag.com 404'
        if r.status_code != 200:
            return 'unknown', 'XboxGamertag.com HTTP ' + str(r.status_code)
        final_path = r.url.rstrip('/').lower()
        expected = ('/search/' + tag).lower()
        body = r.text.lower()
        if final_path.endswith(expected) and ('gamerscore' in body or 'games played' in body or 'profile' in body):
            return 'found', 'XboxGamertag.com profile page'
        # Redirect to homepage or generic search page is treated only as no visible profile.
        if final_path.rstrip('/') in ('https://xboxgamertag.com', 'https://www.xboxgamertag.com') or 'enter a gamertag to lookup' in body:
            return 'not_found', 'XboxGamertag.com no profile page returned'
        return 'unknown', 'XboxGamertag.com response could not be classified'
    except requests.RequestException as e:
        return 'unknown', 'XboxGamertag.com network error: ' + str(e)[:100]


def check_search_index(tag, sess):
    """Search-engine index check. It only detects indexed public pages, not actual Xbox availability."""
    q = 'site:xboxgamertag.com/search/ "' + tag + '"'
    url = 'https://html.duckduckgo.com/html/?q=' + quote_plus(q)
    try:
        r = sess.get(url, timeout=18, headers={'User-Agent': 'Mozilla/5.0 (compatible; WordHunter/1.0)'})
        if r.status_code in (403, 429) or r.status_code >= 500:
            return 'unknown', 'DuckDuckGo blocked/limited HTTP ' + str(r.status_code)
        if r.status_code != 200:
            return 'unknown', 'DuckDuckGo HTTP ' + str(r.status_code)
        soup = BeautifulSoup(r.text, 'html.parser')
        links = soup.select('a.result__a')
        for a in links:
            href = (a.get('href') or '').lower()
            title = a.get_text(' ', strip=True).lower()
            if 'xboxgamertag.com/search/' in href and tag.lower() in title:
                return 'found', 'DuckDuckGo indexed profile result'
        # A valid result page with no exact profile match is weak negative evidence only.
        if 'result' in r.text.lower() or 'no results' in r.text.lower():
            return 'not_found', 'DuckDuckGo no exact indexed profile result'
        return 'unknown', 'DuckDuckGo response could not be classified'
    except requests.RequestException as e:
        return 'unknown', 'DuckDuckGo network error: ' + str(e)[:100]


def run_scan():
    try:
        state['phase'] = '辞書を準備中'
        words = load_words()
        with lock:
            seen = state['seen']
            todo = [w for w in words if w not in seen]
            state['total'] = len(words)
            state['checked'] = len(seen)
        if not todo:
            log('未確認の候補はありません。')
            return
        log(f'{len(todo):,}語を順番に確認します。外部サイトの負荷を避けるため1候補あたり最低4秒待機します。')
        sess = requests.Session()
        sess.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; WordHunter/1.0)'})
        state['phase'] = '外部サイト照合中'
        for tag in todo:
            if stop_event.is_set(): break
            start = time.monotonic()
            a, note_a = check_xboxgamertag_site(tag, sess)
            # Check the second source only when the first did not find a profile.
            if a == 'found':
                result = 'profile_found'
                log(f'除外（プロフィール確認）: {tag}')
            elif a == 'not_found':
                b, note_b = check_search_index(tag, sess)
                if b == 'not_found':
                    result = 'potential'
                    with lock:
                        if tag not in state['potential']:
                            state['potential'].append(tag)
                    log(f'候補（2つの公開照会でプロフィール未検出）: {tag}')
                elif b == 'found':
                    result = 'profile_found_indexed'
                    log(f'除外（検索インデックスにプロフィールあり）: {tag}')
                else:
                    result = 'unknown'
                    log(f'未判定（外部サイト応答不明）: {tag}')
            else:
                result = 'unknown'
                log(f'未判定（外部サイト応答不明）: {tag}')
            with lock:
                state['seen'][tag] = result
                state['checked'] = len(state['seen'])
                state['errors'] = sum(1 for v in state['seen'].values() if v == 'unknown')
            save_state()
            # Avoid sending bursts to the public sites.
            delay = 4.0 - (time.monotonic() - start)
            if delay > 0 and stop_event.wait(delay): break
        log('停止しました。' if stop_event.is_set() else '辞書候補の照合が終了しました。')
    except Exception as e:
        log('エラー: ' + str(e))
    finally:
        with lock:
            state['running'] = False
            state['phase'] = '待機中'
        stop_event.clear()


@app.get('/')
def index():
    return render_template('index.html')

@app.get('/api/status')
def api_status():
    with lock:
        return jsonify({k: v for k, v in state.items() if k != 'seen'} | {'seen_count': len(state['seen']), 'potential': sorted(set(state['potential']))})

@app.post('/api/start')
def api_start():
    global worker
    with lock:
        if state['running']:
            return jsonify({'ok': False, 'message': 'すでにスキャン中です'})
        state['running'] = True
        state['phase'] = '開始中'
    stop_event.clear()
    worker = threading.Thread(target=run_scan, daemon=True)
    worker.start()
    return jsonify({'ok': True})

@app.post('/api/stop')
def api_stop():
    stop_event.set()
    return jsonify({'ok': True, 'message': '停止を要求しました'})

@app.get('/api/export')
def api_export():
    with lock:
        return jsonify({'potential': sorted(set(state['potential'])), 'checked': state['checked'], 'total': state['total'], 'seen_count': len(state['seen'])})

load_state()
if __name__ == '__main__':
    print('\nWordHunter No-Login（公開プロフィール照合版）')
    print('このPCで起動し、同じWi-FiのiPhoneからアクセスしてください。')
    print('URL: http://<PCのIPv4アドレス>:8765')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '8765')), debug=False, threaded=True)
