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

# ファイルをダウンロードし、zipファイルを作成する作業ディレクトリ
TMPPATH='/tmp'

HTTP_CLIENT_CHUNK_SIZE=10240

req = requests.session()
req.headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:43.0) Gecko/20100101 Firefox/43.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
}


def downloadImageFile(dir, imgurl):
    filename = dir + '/' + imgurl.split('/')[-1]
    print( "Download Image File=" + filename )

    for retry in range(1, 10):
        try:
            r = req.get(imgurl, stream=True, timeout=(10.0, 10.0))

            # print 'status_code:' + str(r.status_code)
            length = int(r.headers['Content-Length'])

            if (os.path.exists(filename)) and (os.stat(filename).st_size == length):
                print( 'Used exists file:' + imgurl )
            else:
                # ファイルが存在しない、または、ファイルサイズとダウンロードサイズが異なる。
                with open(filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=4096):
                        if chunk: # filter out keep-alive new chunks
                            f.write(chunk)
                            f.flush()
                    f.close()

            info = os.stat(filename)
            # print 'file size:' + str(info.st_size)
            if info.st_size == length:
                return filename
            else:
                print( 'Download size mismatch file size:' + str(info.st_size) + ' Content-Length:' + length )
                continue

        except requests.exceptions.ConnectionError:
            print( 'ConnectionError:' + imgurl )
            continue
        except requests.exceptions.Timeout:
            print( 'Timeout:' + imgurl )
            continue
        except requests.exceptions.ReadTimeout:
            print( 'Timeout:' + imgurl )
            continue

    # リトライ回数をオーバーで終了
    print( 'Retry over:' + imgurl )
    sys.exit()
#
#

def mkdir(path):
    if not os.path.exists(path):
        os.makedirs(path)

#
#

def zip_dir(dirname,zipfilename):
    filelist = []
    if os.path.isfile(dirname):
        filelist.append(dirname)
    else :
        for root, dirs, files in os.walk(dirname):
            for name in files:
                filelist.append(os.path.join(root, name))

    if (os.path.exists(zipfilename + '.zip')):
        if (os.path.exists(zipfilename + '_.zip')):
            zf = zipfile.ZipFile(zipfilename + '__.zip', "w", zipfile.zlib.DEFLATED)
        else:
            zf = zipfile.ZipFile(zipfilename + '_.zip', "w", zipfile.zlib.DEFLATED)
    else:
        zf = zipfile.ZipFile(zipfilename + '.zip', "w", zipfile.zlib.DEFLATED)

    for tar in filelist:
        arcname = tar[len(dirname):]
        zf.write(tar,arcname)
    zf.close()

#
#

def download_pics(url):
    if ('http://' in url) or ('https://' in url):
        basedir = TMPPATH + '/' + 'tmpimg_' + url.split("/")[-1]
        index = lxml.etree.HTML(req.get(url).text)
        urlbase = ''
    else:
        basedir = TMPPATH + '/' + '123456'
        index = lxml.etree.HTML(codecs.open(url, 'r', 'UTF-8').read())
        urlbase = 'https:'

    title = index.xpath('//div[@id="info"]/h2/text()')
    if not title:
        # 日本語のタイトルが無い場合
        title = index.xpath('//div[@id="info"]/h1/text()')
        if not title:
            title = [str(time.time())]
    title = title[0]
    print(title)
    basename = cleanPath(title)
    print(basedir + '/' + basename)
    mkdir(basedir + '/' + basename)

    AllImgURL = index.xpath('//div[@class="thumb-container"]/a/img')

    for imgtag in AllImgURL:
        picurl = imgtag.attrib['data-src']
        picurl = picurl.replace('t.nhentai.net', 'i.nhentai.net')
        picurl = urlbase + re.sub(r't(.[a-z]+)$', r'\1', picurl)

#        print('picurl=' + picurl)

        downloadImageFile(basedir + '/' + basename, picurl)

    # 圧縮
    zip_dir(basedir + '/' + basename, basename)

    # 作業領域削除
    shutil.rmtree(basedir)

    return True

#
#

def cleanPath(path):
    path = path.strip()  # 文字列の前後の空白を削除
    path = path.replace('|', '')
    path = path.replace(':', '')
    path = path.replace('/', '')
    return path

#
#

if __name__ == '__main__':
    for url in sys.argv[1:]:
        if url[-1] == '/':
            download_pics(url[:-1])
        else:
            download_pics(url)
