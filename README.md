# Apple Books Exporter

用于 macOS 本地批量导出自己导入 Apple Books 的 EPUB/PDF。

## 功能

- 扫描常见 Apple Books 本地与 iCloud Books 目录
- 复制 EPUB/PDF 原文件
- 将 Apple Books 中的 `.epub` 目录重新打包成标准 EPUB
- 提取 EPUB 内部图片
- 提取 PDF 中可提取的栅格图片
- 读取 EPUB 元数据、PDF 元数据与页数
- 生成 `Metadata/catalog.json`、`Metadata/catalog.csv`
- 只读源目录，不删除、不移动、不改写 Apple Books 原始文件

## 环境

macOS + Python 3.10+

```bash
python3 --version
```

推荐使用虚拟环境并安装 PyMuPDF：

```bash
mkdir -p ~/apple-books-exporter
cd ~/apple-books-exporter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pymupdf
```

把 `apple_books_exporter.py` 放入该目录。

## 先扫描，不导出

```bash
python apple_books_exporter.py --dry-run
```

## 正式导出

```bash
python apple_books_exporter.py
```

默认输出：

```text
~/BooksExport/AppleBooks-Export/
```

## 自定义目录

```bash
python apple_books_exporter.py \
  --roots "$HOME/Library/Containers/com.apple.BKAgentService/Data/Documents/iBooks/Books" \
          "$HOME/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents" \
  --output "$HOME/Downloads/AppleBooks-Export"
```

## 覆盖之前由本工具生成的文件

```bash
python apple_books_exporter.py --force
```

## 仅复制 EPUB/PDF，不提取图片

```bash
python apple_books_exporter.py --no-images
```

## 输出结构

```text
AppleBooks-Export/
├── EPUB/
│   └── Book Title--<id>.epub
├── PDF/
│   └── Document Title--<id>.pdf
├── Images/
│   └── Book Title--<id>/
│       ├── EPUB/
│       └── PDF/
├── Metadata/
│   ├── catalog.json
│   └── catalog.csv
└── Logs/
    └── export.log
```

## 权限

如果终端提示 Operation not permitted：
1. 打开 macOS「系统设置」→「隐私与安全性」→「完全磁盘访问权限」。
2. 添加你正在使用的终端应用（Terminal、iTerm 或 VS Code）。
3. 完全退出并重新打开终端，再运行脚本。

## 注意事项

- 建议先在 Books App 中等待 iCloud 图书下载完成。
- 不要在 Finder 中移动、重命名或删除 Books 容器内的原始文件。
- Apple Books 商店购买内容可能受 FairPlay DRM 保护；本工具面向自己导入的无 DRM 文件，不负责绕过 DRM。
- PDF 提取的是可提取的栅格图像；矢量图、页面背景、扫描页等不一定能作为独立图片完整提取。
- EPUB 中的图片按 EPUB 包内路径导出；CSS、字体、音频等其他资源仍保留在原始 EPUB 中，但本工具不单独提取它们。
- 图书的阅读进度、书签、批注和高亮通常不在 EPUB/PDF 文件本身，需要单独处理 Apple Books 数据库。
- 如果 iCloud 文件尚未下载到本地，脚本无法提取尚不存在的本地内容。
