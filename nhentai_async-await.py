#!/usr/bin/env python
# coding: utf-8

import asyncio
import contextlib
import stat

import aiohttp
import lxml.etree
import sys
import re
import os
import zipfile
import shutil
import errno
import logging


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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(threadName)s: %(levelname)s: %(message)s', filename='nHentai.log')
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s %(threadName)s: %(levelname)s: %(message)s'))
logging.getLogger('').addHandler(console)


# ========= HTTP =========

HTTP_HEADERS = {
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

async def fetch_bytes_with_retry(session, url: str, *, retries: int = 10) -> bytes:
    last_exc = None
    for i in range(retries):
        try:
            async with session.get(url) as response:
                if response.status == 404:
                    raise FileNotFoundError(f"404 Not Found: {url}")
                response.raise_for_status()
                return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logging.info("fetch retry %d/%d: %s (%s)", i + 1, retries, url, exc)
            if i + 1 < retries:
                await asyncio.sleep(2)
    raise RuntimeError(f"Retry over: {url}") from last_exc


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


async def downloadImageFile(session, dirpath: str, imgurl: str) -> str | None:
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
            img_bytes = await fetch_bytes_with_retry(session, candidate, retries=10)
            webp_bytes, src_fmt = to_webp_bytes(img_bytes)

            # 既に同サイズ同内容チェック…はコストが高いので、ここでは単純に上書き保存
            with open(out_path, "wb") as f:
                f.write(webp_bytes)

            logging.info("Saved webp ok: %s (src=%s)", out_path, src_fmt)
            return str(out_path)

        except FileNotFoundError as e:
            last_err = e
            logging.error("404 candidate: %s", candidate)
            continue
        except UnsupportedImageFormat as e:
            last_err = e
            logging.error("Unsupported image: %s (%s)", candidate, e)
            continue
        except Exception as e:
            last_err = e
            logging.error("Download/convert failed: %s (%s)", candidate, e)
            continue

    logging.error("All candidates failed: url=%s err=%s", imgurl, last_err)
    return None


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

    logging.info(f"create ZIP file {name=}")
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

async def download_pics(session, tab, url: str):
    basedir = TMPPATH + '/' + 'tmpimg_' + url.split("/")[-1]
    mkdir(basedir)

    for _ in range(0, 5):
        await asyncio.sleep(1)
        await chrome_get(tab, url)
        for _ in range(0, 2):
            HTML = await chrome_getDOM(tab)
            index = lxml.etree.HTML(HTML)

            if index is None:
                logging.info('none index info retry')
                await asyncio.sleep(3)
                continue

            info = index.xpath('//*[@id="info"]/*[contains(@class, "title")]')
            if len(info) == 0:
                logging.info('none title info retry')
                await asyncio.sleep(3)
                continue

            # info配下が一つの場合、英語表記のみ取得、それ以外は日本語表記を取得する。
            if len(info) == 1:
                info = info[0]
            else:
                info = info[1]
            break
        else:
            # 内側の for が break されなかった場合だけ実行される
            continue

        # 内側の for が break された場合ここに来る
        break

    else:
        logging.info('retry out')
        return

    title = info.text if info.text is not None else ""
    for span in info.xpath('span'):
        if span.text and span.text != '[DL版]':
            title = title + span.text
    logging.info(f"{title=}")

    basename = cleanPath(title)
    logging.info(basedir + '/' + basename)
    mkdir(basedir + '/' + basename)

    await asyncio.sleep(2)
    html = await chrome_getDOM(tab)
    await asyncio.sleep(2)
    index = lxml.etree.HTML(html)
    await asyncio.sleep(2)
    AllImgURL = index.xpath('//div[contains(@class,"thumb-container")]/a/img')

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
        await downloadImageFile(session, basedir + '/' + basename, picurl)

    # 圧縮（中身はWebPに統一済み）
    zip_dir(basedir + '/' + basename, basename)

    # 作業領域削除
    shutil.rmtree(basedir)

    return True


# ========= Chrome（非同期 Chrome DevTools Protocol） =========

chrome_app = '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary'


class Chrome:
    def __init__(self, session):
        self.session = session
        self.proc = None
        self.ws = None
        self.serial = 0
        self.user_dir = None
        self.base_url = None
        self.tab_id = None

    async def start(self):
        import tempfile
        self.user_dir = tempfile.mkdtemp(prefix='google-chrome_')
        self.proc = await asyncio.create_subprocess_exec(
            chrome_app, '--user-data-dir=' + self.user_dir,
            '--window-size=800,600', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-port=0',
            'about:blank',stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        port_file = Path(self.user_dir) / 'DevToolsActivePort'
        for _ in range(200):
            if self.proc.returncode is not None:
                raise RuntimeError('Chrome exited during startup')
            try:
                port = int(port_file.read_text().splitlines()[0])
                break
            except (FileNotFoundError, ValueError, IndexError):
                await asyncio.sleep(0.1)
        else:
            raise TimeoutError('Chrome startup timed out')
        self.base_url = f'http://127.0.0.1:{port}'

    @contextlib.asynccontextmanager
    async def new_tab(self):
        """URL一件の処理に専用タブを貸し出し、終了時に閉じる。"""
        if self.base_url is None:
            raise RuntimeError('Chrome has not started')
        if self.tab_id is not None:
            raise RuntimeError('Previous tab is still open')
        try:
            async with self.session.put(self.base_url + '/json/new?about:blank') as response:
                response.raise_for_status()
                tab = await response.json()
                self.tab_id = tab['id']
            self.ws = await self.session.ws_connect(tab['webSocketDebuggerUrl'])
            self.serial = 0
            await self.call('Network.enable')
            yield self
        finally:
            await self.close_tab()

    async def close_tab(self):
        """WebSocketの切断だけではタブは閉じないため、Chromeにも閉鎖を要求する。"""
        try:
            if self.ws is not None:
                await self.ws.close()
        finally:
            self.ws = None
            if self.tab_id is not None:
                async with self.session.get(
                    self.base_url + '/json/close/' + self.tab_id
                ) as response:
                    response.raise_for_status()
                self.tab_id = None

    async def call(self, method, **params):
        # 呼び出し元は単一のconsumer。イベントを読み飛ばし対応する応答を待つ。
        self.serial += 1
        request_id = self.serial
        await self.ws.send_json({'id': request_id, 'method': method, 'params': params})

        async def receive():
            while True:
                message = await self.ws.receive_json()
                if message.get('id') != request_id:
                    continue
                if 'error' in message:
                    raise RuntimeError(str(message['error']))
                return message.get('result', {})
        return await asyncio.wait_for(receive(), timeout=60)

    async def close(self):
        try:
            await self.close_tab()
        except Exception:
            logging.exception('Failed to close tab; shutting down Chrome')
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        if self.user_dir:
            shutil.rmtree(self.user_dir, ignore_errors=True)

    async def clear_site_data(
        self,
        origin: str,
        *,
        clear_http_cache: bool = False,
        clear_http_cookies: bool = False
    ) -> None:
        """現在のタブの対象サイトデータを削除する。"""
        if self.ws is None:
            raise RuntimeError("タブが開かれていません")

        # 現在のタブのセッションストレージ
        await self.call("DOMStorage.enable")
        await self.call(
            "DOMStorage.clear",
            storageId={
                "securityOrigin": origin,
                "isLocalStorage": False,
            },
        )

        # 対象サイトの保存データ
        await self.call(
            "Storage.clearDataForOrigin",
            origin=origin,
            storageTypes=(
                "local_storage,cookies,cache_storage,"
                "service_workers,indexeddb"
            ),
        )

        # Chrome全体のCookieクリア
        if clear_http_cookies:
            await self.call("Network.clearBrowserCookies")

        # Chrome全体のHTTPキャッシュクリア
        if clear_http_cache:
            await self.call("Network.clearBrowserCache")


async def chrome_get(tab, url):
    await tab.call('Page.navigate', url=url)


async def chrome_getDOM(tab):
    for _ in range(10):
        try:
            root = await tab.call('DOM.getDocument')
            html = await tab.call('DOM.getOuterHTML', nodeId=root['root']['nodeId'])
            return html['outerHTML']
        except RuntimeError:
            await asyncio.sleep(1)
    raise RuntimeError('Failed to read Chrome DOM')


# ========= asyncio / FIFO（macOS・Linux用） =========

class PipeReader:
    def __init__(self, path, queue):
        self.path = path
        self.queue = queue
        self.buffer = bytearray()
        self.loop = asyncio.get_running_loop()
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        # writer不在時のEOFによるビジーループを防ぐ。
        self.keepalive = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self.loop.add_reader(self.fd, self.drain)

    def drain(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not data:
                break
            self.buffer.extend(data)
        while b'\n' in self.buffer:
            line, _, remainder = self.buffer.partition(b'\n')
            self.buffer = bytearray(remainder)
            try:
                url = line.decode('utf-8').strip().rstrip('/')
            except UnicodeDecodeError:
                logging.warning('Invalid UTF-8 input ignored')
                continue
            if url:
                self.queue.put_nowait(url)

    def close(self):
        self.loop.remove_reader(self.fd)
        os.close(self.keepalive)
        os.close(self.fd)


async def push_pipe(path, urls):
    # 各行をPIPE_BUF以内の単一writeにし、複数送信元の混在を防ぐ。
    payloads = [(url.strip().rstrip('/') + '\n').encode('utf-8')
                for url in urls if url.strip()]
    if any(b'\n' in data[:-1] or b'\r' in data for data in payloads):
        raise ValueError('URL must not contain newlines')
    fd = None
    for _ in range(100):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            await asyncio.sleep(0.05)
    if fd is None:
        raise RuntimeError(f'FIFO has no reader: {path}')
    try:
        limit = os.fpathconf(fd, 'PC_PIPE_BUF')
        if any(len(data) > limit for data in payloads):
            raise ValueError(f'URL exceeds FIFO atomic write limit ({limit} bytes)')
        for data in payloads:
            while True:
                try:
                    os.write(fd, data)
                    break
                except BlockingIOError:
                    loop = asyncio.get_running_loop()
                    ready = loop.create_future()
                    def writable():
                        if not ready.done():
                            ready.set_result(None)
                    loop.add_writer(fd, writable)
                    try:
                        await asyncio.wait_for(ready, timeout=10)
                    finally:
                        loop.remove_writer(fd)
    finally:
        os.close(fd)


async def consume(queue, reader, process):
    while True:
        url = await queue.get()  # 最初の入力までは終了せず待機する。
        try:
            logging.info(f"start {url=}")
            await process(url)
            logging.info(f"end {url=}")
        finally:
            queue.task_done()
        # 通知コールバックが未実行の入力も終了判定前に取り込む。
        reader.drain()
        if queue.empty():
            # Chrome終了待ちの間に、新規送信を受け付けないよう先に削除。
            os.unlink(reader.path)
            return


async def main(urls=None, *, fifo=FIFO, process=None):
    urls = sys.argv[1:] if urls is None else urls
    try:
        os.mkfifo(fifo, 0o600)
    except FileExistsError:
        if not stat.S_ISFIFO(os.lstat(fifo).st_mode):
            raise RuntimeError(f'Not a FIFO: {fifo}')
        await push_pipe(fifo, urls)
        return

    identity = os.lstat(fifo)
    reader = None
    try:
        queue = asyncio.Queue()
        reader = PipeReader(fifo, queue)
        # 初回起動の引数は直接キューへ。自分のパイプが満杯になるのを防ぐ。
        for url in urls:
            if url.strip():
                queue.put_nowait(url.strip().rstrip('/'))
        if process is not None:  # テスト用の処理差し替え
            await consume(queue, reader, process)
        else:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=10)
            async with aiohttp.ClientSession(headers=HTTP_HEADERS, timeout=timeout) as session:
                chrome = Chrome(session)
                async def process_url(url):
                    if chrome.proc is None:
                        await chrome.start()
                    async with chrome.new_tab() as tab:
                        try:
                            await download_pics(session, tab, url)
                        finally:
                            await tab.clear_site_data(
                               "https://nhentai.net",
                                clear_http_cache=True,
                                clear_http_cookies=True
                            )
                            session.cookie_jar.clear()
                try:
                    await consume(queue, reader, process_url)
                finally:
                    await chrome.close()
    finally:
        # 自分が作成したFIFOだけを削除する。
        try:
            current = os.lstat(fifo)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(fifo)
        except FileNotFoundError:
            pass
        if reader is not None:
            reader.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
