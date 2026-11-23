# hatena-to-astro

はてなブログのエクスポートファイルを、AstroのContent Collectionsで利用できるMarkdown形式に変換するCLIツールです。

記事本文のMarkdown変換や画像のダウンロード、はてなブログ固有の要素の削除を自動で行います。

詳しい仕様や使い方については、ブログ記事をご覧ください。

**[はてなブログからAstroに移行するツールをつくりました](https://blog.moyuru.net/posts/hatena-to-astro-tool)**

以前のバージョンからは全体的にアップデートしています。出力先が`converted`フォルダ固定になったほか、目次やslug、authorの扱いを選べるようになりました。

## 主な機能

- 記事本文のHTMLをMarkdownに変換
- 記事内の画像をダウンロード
- はてなキーワードリンクや埋め込みカードなどを削除
- 見出しから自動生成された目次を削除（残すこともできます）
- 画像のキャプション（figcaption）を画像のaltに反映
- slugやauthorをフロントマターに含めるかどうかを選べる（authorは名前の指定も可能）
- 記事ごとのフォルダに`index.md`を配置するLeaf Bundle形式で、`converted`フォルダに出力
- 公開記事と下書きの変換に対応
- MT形式のTAGSやPRIMARY CATEGORYがあれば、タグとして取り込む
- 日付を日本時間（JST）に変換
- BASENAMEと生成したslugの対応表（`redirect-map.csv`）を出力

## 動作環境

- Python 3.9以上

## インストール

```bash
git clone https://github.com/matcha-soda/hatena-to-astro.git
cd hatena-to-astro
pip install -r requirements.txt
```

## 使い方

### 対話モード（おすすめ）

引数なしで実行すると対話モードが始まります。質問に答えていくだけで変換できます。分からないところは、そのままEnterを押せば既定値が使われます。

```bash
python3 hatena_to_astro.py
```

聞かれるのは次の5つだけです。

1. エクスポートファイルのパス（ファイルをドラッグ＆ドロップで貼り付けてもOK）
2. どの記事を変換するか（公開記事のみ／下書きも含める／下書きのみ）
3. 挿入されている目次を引き継ぐかどうか
4. 記事のURL名を引き継ぐかどうか
5. 著者名を引き継ぐかどうか（引き継ぐ場合は、設定する名前も聞きます。空欄ならエクスポートのAUTHORをそのまま使います）

最後に内容が表示されるので、確認してから変換が始まります。

変換結果は、実行した場所に作られる `converted` フォルダに入ります。あとは、その中身をAstroプロジェクトの記事フォルダ（`src/content/blog` など）へコピーしてください。

### コマンドラインで実行

はてなブログの管理画面から記事をエクスポートし、ダウンロードした`.txt`ファイルを指定して実行します。

```bash
python3 hatena_to_astro.py \
  --input hatena_export.txt
```

出力先は常に、実行した場所の `converted` フォルダです。

オプションを指定して実行することもできます。下書きも含めて変換し、slugとauthorをフロントマターにも書く例です。

```bash
python3 hatena_to_astro.py \
  --input hatena_export.txt \
  --include-drafts \
  --set-slug \
  --author computer-life
```

指定できるオプションは、下の「オプション」の表をご覧ください。

## オプション

| オプション | デフォルト値 | 説明 |
| --- | --- | --- |
| `--input` | なし | はてなブログからエクスポートした .txt ファイルのパス。省略すると対話モードが始まります |
| `--interactive` | なし | 対話モードで起動する |
| `--include-drafts` | なし | 公開記事と下書きの両方を変換する |
| `--drafts-only` | なし | 下書きだけを変換する |
| `--sleep` | `0.2` | 画像ダウンロード後の待機時間（秒） |
| `--no-cleanup` | なし | はてな固有の要素の削除を一部無効化する |
| `--keep-toc` | なし | 見出しから自動生成された目次を残す（既定では削除します） |
| `--set-slug` | なし | slugをフロントマターにも書く（既定ではフォルダ名のみ） |
| `--include-author` | なし | authorをフロントマターに書く（各記事のAUTHORの値を使用） |
| `--author` | なし | authorとして指定した名前を全記事に書く（`--include-author`を兼ねます） |
| `--debug-date` | なし | 最初の記事の日付の解析結果を表示する |

## 出力例

実行した場所に `converted` フォルダが作られ、記事ごとにフォルダを分けて、本文と画像をまとめて保存します。

```text
converted/
├── article-slug/
│   ├── index.md
│   └── image.jpg
└── redirect-map.csv
```

`redirect-map.csv` は、BASENAME、生成したslug、タイトルの対応表です。移行元のURLからリダイレクトを設定する際に使えます。

## 終了コード

| コード | 意味 |
| --- | --- |
| 0 | 正常終了 |
| 1 | 日付を読めなかった記事、記事として認識できずスキップしたブロック、または変換中にエラーが発生した記事があった |
| 2 | 入力ファイルが見つからない |

## 注意事項

- はてな記法や複雑なHTMLは、変換後に表示が崩れる場合があります。
- YouTubeやXなどの埋め込みは、移行先に合わせた修正が必要になる場合があります。
- 変換前に、エクスポートファイルとAstroプロジェクトのバックアップを取ってください。
- 既存の記事フォルダがある状態で再実行すると、`index.md` は上書きされます（上書きした件数は最後に表示されます）。

## ライセンス

MIT License
