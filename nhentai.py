#!/usr/bin/env python
# coding: utf-8

import requests
import lxml.etree
import sys
import re
import os
import zipfile
import shutil
import errno
import select
import logging
from time import sleep
from threading import Thread
from queue import Queue
from subprocess import Popen

import pychrome

# 追加: 画像変換（JPEG/PNG -> WebP）
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps


# ========= 設定 =========

# ファイルをダウンロードし、zipファイルを作成する作業ディレクトリ
TMPPATH = '/tmp'

HTTP_CLIENT_CHUNK_SIZE = 10240

FIFO = '/tmp/nhentai_pipe'

# WebP出力設定（必要なら調整）
WEBP_QUALITY = 40         # JPEG系の変換品質（0-100）
WEBP_METHOD = 6           # WebP圧縮の探索（0-6、6が高圧縮だがCPU使う）
WEBP_LOSSLESS_FOR_PNG = True  # PNG(透過が多い)は基本lossless推奨


# ========= ログ =========

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(threadName)s: %(message)s', filename='nHentai.log')
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s %(threadName)s: %(message)s'))
logging.getLogger('').addHandler(console)


# ========= HTTP =========

req = requests.session()
req.headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:43.0) Gecko/20100101 Firefox/43.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
    'Cache-Control': 'max-age=0',
}


# ========= 画像: 中身判定（拡張子に依存しない） =========

class UnsupportedImageFormat(ValueError):
    """対応外の画像形式（または画像として読めない）"""


def sniff_image_format_from_bytes(head: bytes) -> str:
    """
    画像の先頭バイト列（マジックナンバー）から形式を推定する。
    ここでは最低限 JPEG/PNG/WEBP を判定する。
    """
    if len(head) >= 3 and head[0:3] == b"\xFF\xD8\xFF":
        return "JPEG"
    if len(head) >= 8 and head[0:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    # WebP: "RIFF"...."WEBP"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return "UNKNOWN"


def ensure_parent_dir(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def to_webp_bytes(image_bytes: bytes) -> tuple[bytes, str]:
    """
    画像バイト列を Pillow で読み、WebPへ変換したバイト列を返す。
    - 入力が既に WebP でも「統一」のため再保存する（不要なら分岐でスキップ可能）
    - “拡張子”は見ない。Pillowのデコード結果とシグネチャで判断する。
    戻り値: (webp_bytes, src_format_name)
    """
    # まず先頭で軽く形式推定（ログや分岐のため）
    guessed = sniff_image_format_from_bytes(image_bytes[:16])

    try:
        with Image.open(BytesIO(image_bytes)) as im:
            # スマホ撮影などで EXIF Orientation がある場合、向きを正規化してから保存
            im = ImageOps.exif_transpose(im)

            src_format = (im.format or guessed or "UNKNOWN").upper()

            out = BytesIO()
            save_kwargs = {"format": "WEBP", "method": WEBP_METHOD, "optimize": True}

            # PNGは透過の可能性があるので、基本lossless推奨
            # ※lossless=Falseにしたい場合はWEBP_LOSSLESS_FOR_PNGをFalseに
            if src_format == "PNG":
                save_kwargs["lossless"] = bool(WEBP_LOSSLESS_FOR_PNG)
                if not WEBP_LOSSLESS_FOR_PNG:
                    save_kwargs["quality"] = int(WEBP_QUALITY)
            else:
                # JPEGやその他は基本lossyでOK（必要に応じて調整）
                # ただしRGBA等のままだと互換性でハマることがあるためRGBへ寄せる
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                save_kwargs["quality"] = int(WEBP_QUALITY)

            im.save(out, **save_kwargs)
            return out.getvalue(), src_format

    except Exception as e:
        # 画像として読めない/壊れている等
        raise UnsupportedImageFormat(f"Failed to decode/convert image: {e}") from e


# ========= 画像: ダウンロード（取得） =========

def fetch_bytes_with_retry(url: str, *, retries: int = 10) -> bytes:
    """
    URLからコンテンツを取得して bytes で返す。
    - タイムアウト/一時障害はリトライ
    - 404は上位でURL差し替えしたいケースがあるため例外化して扱う
    """
    last_exc = None
    for i in range(retries):
        try:
            r = req.get(url, stream=True, timeout=(10.0, 10.0))
            if r.status_code == 404:
                raise FileNotFoundError(f"404 Not Found: {url}")
            r.raise_for_status()

            # stream=Trueだが、ここではメモリにまとめる（画像は通常そこまで巨大ではない想定）
            # 巨大ファイルがあるなら一旦tmpに落としてから変換する方式へ変更可能
            buf = BytesIO()
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    buf.write(chunk)
            return buf.getvalue()

        except FileNotFoundError as e:
            # 404は即上位へ（URL差し替えしたい）
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as e:
            last_exc = e
            logging.info("fetch retry %d/%d: %s (%s)", i + 1, retries, url, type(e).__name__)
            sleep(2)
            continue

    raise RuntimeError(f"Retry over: {url} last={last_exc}")


# ========= 画像: ダウンロード + WebP保存（統一） =========

def derive_webp_filename(dirpath: str | Path, imgurl: str) -> Path:
    """
    URL末尾のファイル名をベースに「.webp」へ置換した保存先パスを作る。
    例: .../1.jpg  -> 1.webp
        .../10.png -> 10.webp
        .../3.webp -> 3.webp
    ※ここでは“拡張子に依存しない判定”をするが、保存名は人間が見やすいようにURL末尾を利用。
    """
    dirpath = Path(dirpath)
    base = imgurl.split("/")[-1]
    # クエリが付く場合は落とす
    base = base.split("?")[0]
    stem = Path(base).stem
    return dirpath / f"{stem}.webp"


def downloadImageFile(dirpath: str, imgurl: str) -> str:
    """
    1ページ分のイメージを取得し、必ずWebPとして保存する。
    - 取得元が JPEG/PNG/WebP のどれでもOK（中身で判定して変換）
    - 404時は元コード同様にURLの拡張子パターンを差し替えて再試行
    戻り値: 保存した WebP ファイルパス（文字列）
    """
    out_path = derive_webp_filename(dirpath, imgurl)
    ensure_parent_dir(out_path)

    logging.info("Download+Convert -> WebP: url=%s out=%s", imgurl, out_path)

    # 404のときに差し替える候補を順に試す（元コードの挙動を踏襲）
    url_candidates = [imgurl]

    # nhentaiは .jpg.webp のような表記揺れがあり得るので、失敗したら戻す
    # ※“候補”として積むだけで、最終的に中身で判定してWebPへ変換する
    if ".png.webp" in imgurl or ".jpg.webp" in imgurl or ".jpeg.webp" in imgurl:
        url_candidates.append(imgurl.replace(".png.webp", ".png").replace(".jpg.webp", ".jpg").replace(".jpeg.webp", ".jpeg"))

    # さらに、.webp から jpg/pngへ戻す候補を作る（実運用で困ることがあるため）
    if imgurl.endswith(".webp"):
        url_candidates.append(imgurl[:-5] + ".jpg")
        url_candidates.append(imgurl[:-5] + ".png")
        url_candidates.append(imgurl[:-5] + ".jpeg")

    # 実行（候補を順に試し、取得できたバイト列をWebP化して保存）
    last_err = None
    for candidate in url_candidates:
        try:
            img_bytes = fetch_bytes_with_retry(candidate, retries=10)
            webp_bytes, src_fmt = to_webp_bytes(img_bytes)

            # 既に同サイズ同内容チェック…はコストが高いので、ここでは単純に上書き保存
            with open(out_path, "wb") as f:
                f.write(webp_bytes)

            logging.info("Saved webp ok: %s (src=%s)", out_path, src_fmt)
            return str(out_path)

        except FileNotFoundError as e:
            last_err = e
            logging.info("404 candidate: %s", candidate)
            continue
        except UnsupportedImageFormat as e:
            last_err = e
            logging.info("Unsupported image: %s (%s)", candidate, e)
            continue
        except Exception as e:
            last_err = e
            logging.info("Download/convert failed: %s (%s)", candidate, e)
            continue

    logging.info("All candidates failed: url=%s err=%s", imgurl, last_err)
    sys.exit()


# ========= ユーティリティ =========

def mkdir(path):
    if not os.path.exists(path):
        os.makedirs(path)


def zip_dir(dirname, zipfilename):
    """
    指定ディレクトリ配下のファイルをcbz(zip)にまとめる。
    ※今回「WebPに統一」するため、基本的に中身は .webp のみになる想定
    """
    filelist = []
    if os.path.isfile(dirname):
        filelist.append(dirname)
    else:
        for root, dirs, files in os.walk(dirname):
            for name in files:
                filelist.append(os.path.join(root, name))

    # 同じファイルのzipファイルが存在するかチェック
    r = ''
    name = zipfilename + r + '.cbz'
    for _ in range(0, 10):
        if os.path.exists(name):
            r = r + '_'
            name = zipfilename + r + '.cbz'
            continue
        else:
            break

    zf = zipfile.ZipFile(name, "w", zipfile.zlib.DEFLATED)

    for tar in sorted(filelist):
        # zip内の相対パス（先頭スラッシュを避ける）
        arcname = tar[len(dirname):].lstrip(os.sep)
        zf.write(tar, arcname)
    zf.close()


def cleanPath(path):
    """
    ファイル名に使用できない、使用しない方がいい文字を削除
    """
    path = path.strip()
    path = path.replace('|', '')
    path = path.replace(':', '')
    path = path.replace('/', '')
    return path


# ========= メイン処理（ページ一覧取得→画像DL→圧縮） =========

def download_pics(tab, url: str):
    basedir = TMPPATH + '/' + 'tmpimg_' + url.split("/")[-1]
    mkdir(basedir)

    sleep(1)
    chrome_get(tab, url)
    for _ in range(0, 100):
        HTML = chrome_getDOM(tab)
        index = lxml.etree.HTML(HTML)

        if index is None:
            logging.info('none index info retry')
            sleep(3)
            continue

        info = index.xpath('//*[@id="info"]/*[@class="title"]')
        if len(info) == 0:
            logging.info('none title info retry')
            sleep(3)
            continue

        # info配下が一つの場合、英語表記のみ取得、それ以外は日本語表記を取得する。
        if len(info) == 1:
            info = info[0]
        else:
            info = info[1]
        break
    else:
        logging.info('retry out')
        return

    title = info.text
    for span in info.xpath('span'):
        if span.text and span.text != '[DL版]':
            title = title + span.text
    logging.info(f"{title=}")

    basename = cleanPath(title)
    logging.info(basedir + '/' + basename)
    mkdir(basedir + '/' + basename)

    sleep(2)
    html = chrome_getDOM(tab)
    sleep(2)
    index = lxml.etree.HTML(html)
    sleep(2)
    AllImgURL = index.xpath('//div[@class="thumb-container"]/a/img')

    for imgtag in AllImgURL:
        data_src = imgtag.attrib.get('data-src', '')
        src = imgtag.attrib.get('src', '')
        picurl = imgtag.attrib.get('data-src', '') or imgtag.attrib.get('src', '')

        if not picurl:
            continue

        if picurl[0:6] != "https:":
            picurl = "https:" + picurl

        # サムネイル(tX)から実画像(iX)へ
        picurl = re.sub(r't([0-9]*).nhentai.net', r'i\1.nhentai.net', picurl)

        # 表記揺れ/重複拡張子の整理（ここではURL整形のみ）
        picurl = re.sub(r'\.webp\.webp', r'.webp', picurl)
        picurl = re.sub(r'\.jpeg\.webp', r'.jpeg', picurl)
        picurl = re.sub(r'\.jpg\.webp', r'.jpg', picurl)
        picurl = re.sub(r'([0-9]+)t\.', r'\1.', picurl)

        logging.info(f"{data_src=}, {src=}, {picurl=}")

        # 1ページ分のイメージを取得（必ずWebPとして保存）
        downloadImageFile(basedir + '/' + basename, picurl)

    # 圧縮（中身はWebPに統一済み）
    zip_dir(basedir + '/' + basename, basename)

    # 作業領域削除
    shutil.rmtree(basedir)

    return True


# ========= Chrome / pychrome =========

user_dir = os.path.join(os.environ.get('TEMP', '/tmp'), 'google-chrome_{0:08d}'.format(os.getpid()))

# chrome_app = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
chrome_app = '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary'


def chrome_start(headless=False):
    try:
        os.makedirs(user_dir, exist_ok=False)
    except FileExistsError:
        pass

    opt = [chrome_app]
    opt.append('--user-data-dir=' + user_dir)
    if headless:
        opt.append('--window-size=800,600')
    else:
        opt.append('--window-size=800,600')
    opt.append('--no-first-run')
    opt.append('--no-default-browser-check')
    opt.append('--homepage=about:blank')

    opt.append('--remote-allow-origins=http://127.0.0.1:19222')
    opt.append('--remote-debugging-port=19222')

    proc = Popen(opt)

    sleep(10)

    browser = pychrome.Browser(url="http://127.0.0.1:19222")

    tab = browser.new_tab()
    tab.start()
    tab.Network.enable()

    return browser, proc, tab


def chrome_stop(browser, proc, tab) -> None:
    tab.stop()
    tab.wait(5)
    browser.close_tab(tab)
    proc.kill()
    shutil.rmtree(user_dir)


def chrome_get(tab, url: str) -> None:
    tab.Page.navigate(url=url, _timeout=60)


def chrome_getDOM(tab) -> str:
    for _ in range(0, 10):
        try:
            root = tab.DOM.getDocument()
            HTML = tab.DOM.getOuterHTML(nodeId=root['root']['nodeId'])
            return HTML['outerHTML']
        except pychrome.exceptions.CallMethodException:
            sleep(1)
    else:
        return None


# ========= スレッド / FIFO =========

def download_thread(queue, cqueue):
    """
    ダウンロードスレッド:
      キューからURLを読み取り、各ギャラリーの画像をDLしてcbz化する。
    """
    (browser, proc, tab) = chrome_start(True)

    while True:
        url = queue.get()
        logging.info('get queue url %s', url)
        if url is None:
            queue.task_done()
            break

        download_pics(tab, url)

        queue.task_done()
        cqueue.get()

    chrome_stop(browser, proc, tab)


def read_thread(queue, cqueue):
    """
    パイプリードスレッド:
      FIFOからURLを読み取り、ダウンロードキューへ投入する。
    """
    r_fd = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
    read_pipe = os.fdopen(r_fd, 'r')
    remove = False

    while True:
        rfd, _, _ = select.select([r_fd], [], [], 5)

        # タイムアウト（5秒）判定
        if len(rfd) == 0:
            # pipe削除済みかつタイムアウト → 終了
            if remove:
                break

            # 処理中キューが空ならFIFO削除し、以後はタイムアウトで抜ける
            if cqueue.empty():
                os.remove(FIFO)
                remove = True
                continue
        else:
            url = read_pipe.readline().replace('\n', '')
            if len(url) == 0:
                continue

            logging.info('put queue url %s', url)
            queue.put(url)
            cqueue.put('x')

    # download threadに終了を通知
    queue.put(None)
    queue.join()


def run_thread():
    """
    FIFO作成とスレッド起動。
    既にFIFOが存在する場合は起動せず復帰。
    """
    try:
        os.mkfifo(FIFO, 0o777)

        q = Queue()
        cq = Queue()

        Thread(target=download_thread, args=([q, cq])).start()

        rt = Thread(target=read_thread, args=([q, cq]))
        rt.start()

        return rt
    except OSError as oe:
        if oe.errno != errno.EEXIST:
            raise
    return None


def push_pipe():
    """
    引数で指定するURLをFIFOに書き込む
    """
    w_fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    write_pipe = os.fdopen(w_fd, 'w')

    for url in sys.argv[1:]:
        logging.info('put pipe url %s', url)
        if url[-1] == '/':
            write_pipe.write(url[:-1] + '\n')
        else:
            write_pipe.write(url + '\n')

    write_pipe.close()


def main():
    rt = run_thread()

    sleep(1)
    push_pipe()

    if rt is not None:
        rt.join()


if __name__ == '__main__':
    try:
        main()
    except OSError as oe:
        if oe.errno == errno.ENXIO:
            # pipeはあるがreadが開いていない → 一度削除してやり直し
            logging.info('download retry')
            os.remove(FIFO)
            main()
