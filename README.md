# hatena-to-astro

はてなブログのエクスポートファイルを、AstroのContent Collectionsで利用できるMarkdown形式に変換するCLIツールです。

記事本文のMarkdown変換や画像のダウンロード、はてなブログ固有の要素の削除を自動で行います。

詳しい仕様や使い方については、ブログ記事をご覧ください。

**[はてなブログからAstroに移行するツールをつくりました](https://blog.moyuru.net/posts/hatena-to-astro-tool)**

## 主な機能

- 記事本文のHTMLをMarkdownに変換
- 記事内の画像をダウンロード
- はてなキーワードリンクや埋め込みカードなどを削除
- 記事ごとのフォルダに`index.md`を配置するLeaf Bundle形式で出力
- 公開記事と下書きの変換に対応
- 日付を日本時間（JST）に変換

## 動作環境

- Python 3.9以上

## インストール

```bash
git clone https://github.com/matcha-soda/hatena-to-astro.git
cd hatena-to-astro
pip install -r requirements.txt
```

## 使い方

はてなブログの管理画面から記事をエクスポートし、ダウンロードした`.txt`ファイルを指定して実行します。

```bash
python3 hatena_to_astro.py \
  --input hatena_export.txt
```

`--input`にはエクスポートファイルのパスを指定します。
適宜変更してください。

## オプション

| オプション | デフォルト値 | 説明 |
| --- | --- | --- |
| `--input` | 必須 | エクスポートファイルのパス |
| `--astro-dir` | `.` | Astroプロジェクトのルートディレクトリ |
| `--posts-dir` | `src/content/blog` | 記事の出力先 |
| `--include-drafts` | なし | 公開記事と下書きの両方を変換 |
| `--drafts-only` | なし | 下書きだけを変換 |
| `--sleep` | `0.2` | 画像ダウンロード後の待機時間（秒） |
| `--no-cleanup` | なし | はてな固有の要素の削除を一部無効化 |
| `--debug-date` | なし | 最初の記事の日付の解析結果を表示 |

## 出力例

デフォルトでは、記事ごとにフォルダを作成し、本文と画像をまとめて保存します。

```text
src/content/blog/
└── article-slug/
    ├── index.md
    └── image.jpg
```

## 注意事項

- はてな記法や複雑なHTMLは、変換後に表示が崩れる場合があります。
- YouTubeやXなどの埋め込みは、移行先に合わせた修正が必要になる場合があります。
- 変換前に、エクスポートファイルとAstroプロジェクトのバックアップを取ってください。

## ライセンス

MIT License
