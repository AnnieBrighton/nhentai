# nhentai ダウンローダ

## 環境
  MAC(おそらくLinux環境でも動くかと)
  Python 3.5/2.7

## 使い方
### 単一指定

     ./nhentai.py 'https://nhentai.net/g/193411/'

正常にダウンロードできた場合、カレントディレクトリにzipファイルを作成

     [ふる屋 (TAKE)] Remレムのエロ漫画 (Reゼロから始める異世界生活) [DL版].zip

### 複数指定

ダウンロードしたいリストファイルを作成

sample.txt

    https://nhentai.net/g/193080/
    https://nhentai.net/g/192773/

ダウンロード実行

    ./nhentai.sh sample.txt
