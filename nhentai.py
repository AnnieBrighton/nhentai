#!/usr/bin/env python
# coding: utf-8

import requests
import lxml.etree
import sys
import codecs
import re
import time
import os
import zipfile
import getopt
import shutil
import errno
import select
import logging
from time import sleep
from threading import Thread
from queue import Queue

# ファイルをダウンロードし、zipファイルを作成する作業ディレクトリ
TMPPATH = '/tmp'

HTTP_CLIENT_CHUNK_SIZE = 10240

FIFO = '/tmp/nhentai_pipe'

logging.basicConfig(level=logging.INFO, format='%(threadName)s: %(message)s')

req = requests.session()
req.headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:43.0) Gecko/20100101 Firefox/43.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
}

#
# 1ページ分のイメージを取得
#


def downloadImageFile(dir, imgurl):
    filename = dir + '/' + imgurl.split('/')[-1]
    logging.info('Download Image File=%s', filename)

    for retry in range(0, 10):
        try:
            r = req.get(imgurl, stream=True, timeout=(10.0, 10.0))

            # print 'status_code:' + str(r.status_code)
            length = int(r.headers['Content-Length'])

            if (os.path.exists(filename)) and (os.stat(filename).st_size == length):
                logging.info('Used exists file=%s', imgurl)
            else:
                # ファイルが存在しない、または、ファイルサイズとダウンロードサイズが異なる。
                with open(filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=4096):
                        if chunk:  # filter out keep-alive new chunks
                            f.write(chunk)
                            f.flush()
                    f.close()

            info = os.stat(filename)
            # print 'file size:' + str(info.st_size)
            if info.st_size == length:
                return filename
            else:
                logging.info('Download size mismatch file size:%d Content-Length:%d', info.st_size, length)
                continue

        except requests.exceptions.ConnectionError:
            logging.info('ConnectionError:%s', imgurl)
            continue
        except requests.exceptions.Timeout:
            logging.info('Timeout:%s', imgurl)
            continue
        except requests.exceptions.ReadTimeout:
            logging.info('ReadTimeout:%s', imgurl)
            continue

    # リトライ回数をオーバーで終了
    logging.info('Retry over:%s', imgurl)
    sys.exit()

#
# ディレクトリ作成
#


def mkdir(path):
    if not os.path.exists(path):
        os.makedirs(path)

#
# zipファイルに圧縮
#


def zip_dir(dirname, zipfilename):
    filelist = []
    if os.path.isfile(dirname):
        filelist.append(dirname)
    else:
        for root, dirs, files in os.walk(dirname):
            for name in files:
                filelist.append(os.path.join(root, name))

    # 同じファイルのzipファイルが存在するかチェック
    r = ''
    for i in range(0, 10):
        name = zipfilename + r + '.zip'
        if (os.path.exists(name)):
            r = r + '_'
            continue
        else:
            break

    zf = zipfile.ZipFile(name, "w", zipfile.zlib.DEFLATED)

    for tar in filelist:
        arcname = tar[len(dirname):]
        zf.write(tar, arcname)
    zf.close()

#
# ファイル名に使用できない、使用しない方がいい文字を削除
#


def cleanPath(path):
    path = path.strip()  # 文字列の前後の空白を削除
    path = path.replace('|', '')
    path = path.replace(':', '')
    path = path.replace('/', '')
    return path


#
#
#


def download_pics(url):
    title = ''
    basedir = TMPPATH + '/' + 'tmpimg_' + url.split("/")[-1]
    mkdir(basedir)

    for retry in range(0, 11):
        # タイトル・イメージリストの取得に失敗した場合終了する
        if retry == 10:
            logging.info('Title and image list get error:%s', url)
            return False

        try:
            index = lxml.etree.HTML(req.get(url).text)
        except requests.exceptions.ConnectionError:
            logging.info('ConnectionError:%s', url)
            continue

        # < divid = "info" >
        #  < h1 class = "title" >
        #    <span class="before"></span>
        #    <span class="pretty">COMIC Ananga Ranga Vol. 60</span>
        #    <span class="after"></span>
        #   </h1>
        #  <h2 class="title">
        #   <span class="before"></span>
        #   <span class="pretty">アナンガ・ランガ Vol. 60</span>
        #   <span class="after"></span>
        #  </h2>

        info = index.xpath('//*[@id="info"]/*[@class="title"]')
        if len(info) == 0:
            # タイトルが取得出来ない場合、htmlの取得に失敗している可能性のためリトライを行う
            continue

        # info配下が一つの場合、英語表記のみ取得、それ以外は日本語表記を取得する。
        if len(info) == 1:
            info = info[0]
        else:
            info = info[1]

        #print(lxml.etree.tostring(info, pretty_print=True))
        for span in info.xpath('span'):
            #print(lxml.etree.tostring(span, pretty_print=True))
            if span.text and span.text != '[DL版]':
                title = title + span.text
        print(title)

        break

    basename = cleanPath(title)
    print(basedir + '/' + basename)
    mkdir(basedir + '/' + basename)

    AllImgURL = index.xpath('//div[@class="thumb-container"]/a/img')

    for imgtag in AllImgURL:
        picurl = imgtag.attrib['data-src']
        picurl = re.sub(r't([0-9]*).nhentai.net', r'i\1.nhentai.net', picurl)
        picurl = re.sub(r't(.[a-z]+)$', r'\1', picurl)
        # <a class="gallerythumb" href="/g/394364/1/" rel="nofollow">
        #   <img class="lazyload" width="200" height="284" data-src="https://t5.nhentai.net/galleries/2157708/1t.jpg" src="https://t3.nhentai.net/galleries/2157708/1t.jpg">
        #   <noscript>
        #   <img src="https://t5.nhentai.net/galleries/2157708/1t.jpg" width="200" height="284"  />
        #   </noscript>
        # </a>

        # //*[@id="image-container"]/a/img
        # <section id="image-container" class="fit-horizontal full-height zoom-100">
        #   <a href="/g/394364/2/">
        #     <img src="https://i5.nhentai.net/galleries/2157708/1.jpg" width="1055" height="1500">
        #   </a>
        # </section>
#        print('picurl=' + picurl)

        # 1ページ分のイメージを取得
        downloadImageFile(basedir + '/' + basename, picurl)

    # 圧縮
    zip_dir(basedir + '/' + basename, basename)

    # 作業領域削除
    shutil.rmtree(basedir)

    return True


def download_thread(queue, cqueue):
    """
    ・ダウンロードスレッド … キューからURLを読み取り、URLイメージをダウンロードする。
    """
    while True:
        # キューからURLを取り出す。
        url = queue.get()
        logging.info('get queue url %s', url)
        if url is None:
            # キューの処理通知
            queue.task_done()
            break

        # 取り出したURLのダウンロード
        download_pics(url)

        # キューの処理通知
        queue.task_done()
        cqueue.get()


def read_thread(queue, cqueue):
    """
    ・パイプリードスレッド … パイプからURLを読み取り、キューに格納する。
    """
    #logging.info('Opening FIFO...')
    r_fd = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
    #logging.info('FIFO read opened')
    read_pipe = os.fdopen(r_fd, 'r')
    remove = False
    while True:
        rfd, _, _ = select.select([r_fd], [], [], 5)

        # Time out check
        if len(rfd) == 0:
            # pipeファイル削除後のタイムアウトならば、パイプからの読み込みを終了
            if remove:
                break

            # 処理中キューが空
            if cqueue.empty():
                os.remove(FIFO)
                remove = True
                continue
        else:
            url = read_pipe.readline().replace('\n', '')
            if len(url) == 0:
                continue

            # キューにURLを格納
            logging.info('put queue url %s', url)
            queue.put(url)
            cqueue.put('x')

    # download threadに終了を通知
    queue.put(None)
    queue.join()


def run_thread():
    """
    パイプファイル、キューの作成 、 スレッドの起動
    すでにパイプファイルが存在する場合 、 スレッドは起動せず復帰する 。
    ・パイプリードスレッド … パイプからURLを読み取り、キューに格納する。
    ・ダウンロードスレッド … キューからURLを読み取り、URLイメージをダウンロードする。
    """
    try:
        #logging.info('create FIFO')

        # パイプ作成
        os.mkfifo(FIFO, 0o777)

        # キュー作成
        q = Queue()
        cq = Queue()

        # ダウンロードスレッド起動
        Thread(target=download_thread, args=([q, cq])).start()

        # パイプからURL取得スレッド起動
        rt = Thread(target=read_thread, args=([q, cq]))
        rt.start()

        return rt
    except OSError as oe:
        if oe.errno != errno.EEXIST:
            raise
    return None


def push_pipe():
    """
    引数で指定するURLをパイプファイルに書き込む
    """
    w_fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    #logging.info('FIFO write opened')
    write_pipe = os.fdopen(w_fd, 'w')

    for url in sys.argv[1:]:
        logging.info('put pipe url %s', url)
        if url[-1] == '/':
            write_pipe.write(url[:-1] + '\n')
        else:
            write_pipe.write(url + '\n')

    write_pipe.close()

#
# メイン
#


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
            # pipeファイルが存在するが、readでオープンされていない場合
            # 一度、pipeファイルを削除し、最初からやり直す。
            logging.info('download retry')
            os.remove(FIFO)
            main()
