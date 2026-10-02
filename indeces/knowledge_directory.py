"""A local directory entry point, without scanning or changing raw materials."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from .config import PdfConfig
from .path_policy import PathPolicyError, validate_knowledge_root


MANAGED_DIRECTORY = ".indeces"
GUIDE_FILENAME = "README.md"
STAGING_DIRECTORY = "_staging"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".md", ".markdown", ".txt"})
RESERVED_ROOT_DIRECTORIES = frozenset({MANAGED_DIRECTORY, STAGING_DIRECTORY})
MAX_GUIDE_BYTES = 16 * 1024
GUIDE = """# Indeces 知识库目录

本目录保存你提供的原始资料。可使用主题、年份等子目录；文件按原路径入库。
支持 PDF、Markdown（.md / .markdown）和文本（.txt）。其他格式可以保存在这里，
但目前不参与转换、标词或检索。目录入口不自动移动、删除、重新命名或上传原文件。
改名或移动文件会被监听器视为原来源删除、另一条路径新增；不会自动合并来源。

Console 输入 `knowledge` 查看绝对路径和当前导入限制；Windows 会打开此目录。
只有用户显式 `start` 启动服务后，监听器才持续处理更新。打开目录不会启动服务、
模型或 Discord。PDF 先在本机独立进程转换为 Markdown，审阅稿保存于配置的
state 目录下 `pdf_markdown/`，不会回写或替换这里的原始 PDF。
随后按原切块策略后台标词；新版本全部完成后才发布。合法替换在处理期间或失败
后仍使用已有完整发布版本，使用 `knowledge` 独立窗口查看进度和完成状态。删除或超限会按撤下策略
处理，不保留为在线证据。聊天本身不标词、不自动入库。

本目录不是无限量在线索引。默认支持候选上限为 256 个，以 Console 显示的配置
为准。候选数量超过当前上限时，监听器会撤下并归档当前知识范围的全部来源；不会只取
前 256 个，也不会自动分批或扩大预算。PDF 与 Markdown/文本有各自的单文件和
处理限制，请先确认 Console 显示的限额，再准备需要在线检索的资料。
超过候选上限后，即使恢复到上限以内，也需要重新转换和标词，不会直接恢复旧快照。

根目录 `_staging/` 是原文件暂存区，不扫描、不标词、不检索，也不计入支持候选
上限。超量或尚不打算检索的资料可留在其中；暂存能力不等于无限扫描或入库能力。
需要入库时由你把选定文件移到暂存区之外；不会自动整理或搬运。
已入库文件移进根级 `_staging/` 后，会从当前检索撤下并归档，原文件仍保存在暂存区。
根目录的 `_staging/` 和 `.indeces/` 才是保留区域，主题子目录中的同名目录仍
属于正常材料路径。暂存区没有自动生成的材料 README，不会混入搬出的资料。

`.indeces/` 是专属管理目录，不参与知识扫描。请把材料放在它之外。
此 README 只有固定说明，不保存私有材料内容、文件名清单、统计或运行记录。
已有自定义 README 会保留，不会被目录入口覆盖。
原材料与知识历史不受 scratch 滚动 24 小时清理；scratch 的保留策略是独立的。

知识文件是私有运行数据，不应提交或上传 GitHub。仓库默认排除 `knowledge/`
中的资料；仅使用项目的 knowledge 子目录；OneDrive、外部目录和目录链接会被拒绝。不要强制添加原材料。
"""


class KnowledgeDirectoryError(ValueError):
    """Fixed diagnostics without raw file contents or arbitrary I/O messages."""


def _check_type(path: Path, *, directory: bool):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or reparse or not wanted:
        raise KnowledgeDirectoryError("knowledge_directory_unsafe_path")
    return info


def _check_directory_chain(path: Path):
    for component in reversed((path, *path.parents)):
        _check_type(component, directory=True)


def _write_fixed_guide(target: Path, content: bytes):
    """Publish a completed private file without ever replacing an existing guide."""
    _check_directory_chain(target.parent)
    if _check_type(target, directory=False) is not None:
        return
    descriptor = None
    temporary = None
    created = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".knowledge-guide-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        _check_directory_chain(target.parent)
        created = _check_type(temporary, directory=False)
        opened = os.fstat(descriptor)
        if created is None or not stat.S_ISREG(opened.st_mode) or \
                (created.st_dev, created.st_ino) != (opened.st_dev, opened.st_ino):
            raise KnowledgeDirectoryError("knowledge_guide_path_changed")
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        _check_directory_chain(target.parent)
        current = _check_type(temporary, directory=False)
        if current is None or (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino):
            raise KnowledgeDirectoryError("knowledge_guide_path_changed")
        if _check_type(target, directory=False) is not None:
            return
        try:
            # Same-directory hard linking is atomic and fails if the target exists.
            # An unsupported filesystem must fail rather than fall back to overwrite.
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _check_directory_chain(target.parent)
            if _check_type(target, directory=False) is None:
                raise KnowledgeDirectoryError("knowledge_guide_path_changed")
            return
        _check_directory_chain(target.parent)
        current = _check_type(target, directory=False)
        if current is None or (created.st_dev, created.st_ino) != (current.st_dev, current.st_ino):
            raise KnowledgeDirectoryError("knowledge_guide_path_changed")
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            # Never remove target: another editor may now own it, even if fsync
            # or publication failed. Clean only our still-identical private inode.
            if temporary is not None and created is not None:
                try:
                    _check_directory_chain(target.parent)
                    current = _check_type(temporary, directory=False)
                    if current is not None and (created.st_dev, created.st_ino) == (current.st_dev, current.st_ino):
                        temporary.unlink()
                except (OSError, KnowledgeDirectoryError):
                    pass


def _configured_root(config) -> Path:
    try:
        root = validate_knowledge_root(config)
    except PathPolicyError as error:
        raise KnowledgeDirectoryError(error.code) from None
    if any(ord(character) < 32 or ord(character) == 127 for character in str(root)):
        raise KnowledgeDirectoryError("knowledge_directory_invalid_path")
    return root


def _existing_safe_root(config) -> Path:
    """Validate only metadata for a read-only fallback; create or read no files."""
    root = _configured_root(config)
    _check_directory_chain(root)
    if _check_type(root, directory=True) is None:
        raise KnowledgeDirectoryError("knowledge_directory_unavailable")
    _check_directory_chain(root / STAGING_DIRECTORY)
    managed = root / MANAGED_DIRECTORY
    _check_directory_chain(managed)
    _check_type(managed / GUIDE_FILENAME, directory=False)
    return root


def prepare_knowledge_directory(config) -> Path:
    """Validate the project knowledge root; create only the fixed directory guide."""
    root = _configured_root(config)
    content = GUIDE.encode("utf-8")
    if len(content) > MAX_GUIDE_BYTES:
        raise KnowledgeDirectoryError("knowledge_guide_size_limit")
    _check_directory_chain(root)
    root.mkdir(parents=True, exist_ok=True)
    _check_directory_chain(root)
    staging = root / STAGING_DIRECTORY
    _check_directory_chain(staging)
    staging.mkdir(exist_ok=True)
    _check_directory_chain(staging)
    managed = root / MANAGED_DIRECTORY
    _check_directory_chain(managed)
    managed.mkdir(exist_ok=True)
    _check_directory_chain(managed)
    _write_fixed_guide(managed / GUIDE_FILENAME, content)
    return root


def show_knowledge_directory(config, *, open_directory=True) -> Path:
    preparation_error = None
    try:
        root = prepare_knowledge_directory(config)
    except OSError as error:
        # A readable source directory may not permit fixed guide creation.
        # Revalidate every relevant path before offering metadata-only access.
        # Unsafe-path or other programming/configuration errors still propagate.
        root = _existing_safe_root(config)
        preparation_error = type(error).__name__
    knowledge = config.knowledge
    pdf = getattr(config, "pdf", PdfConfig())
    print(f"Indeces 知识库目录：{root}")
    if preparation_error is not None:
        print(f"管理说明未准备：{preparation_error}；仍可访问原目录，本次不会重试写入。")
    print("支持 PDF / Markdown (.md, .markdown) / 文本 (.txt)，可放入主题或年份子目录。")
    print(f"Markdown/文本：单文件 {knowledge.max_file_bytes} bytes；支持候选上限 {knowledge.max_files}；"
          f"每块 {knowledge.chunk_characters} 字符。超出候选上限会撤下并归档当前知识范围全部来源。")
    print(f"PDF：单文件 {pdf.max_file_bytes} bytes；{pdf.max_pages} 页；转换 Markdown {pdf.max_markdown_bytes} bytes；"
          f"转换 {pdf.seconds} 秒。")
    print(f"整篇标词：输入 {knowledge.version_input_tokens} tokens；输出 {knowledge.version_output_tokens} tokens；"
          f"总时间 {knowledge.version_seconds} 秒。各阶段原有预算不扩额。")
    print("服务显式 start 后才转换和后台标词；进度与发布结果在 knowledge 窗口查看，回执保留于 scratch。打开目录不会启动服务。")
    print("原文件不自动移动、删除、重新命名或上传；改名/移动视为原来源删除和新来源新增。")
    staging_label = "未入库暂存区" if preparation_error is None else "暂存区目标（本次未准备，可能不存在）"
    print(f"{staging_label}：{root / STAGING_DIRECTORY}；不扫描、不标词、不计候选上限。超量 raw 请留在此处。")
    print("暂存容量不代表无限扫描/入库；仅根级 _staging 和 .indeces 被排除，主题子目录中的同名目录仍按材料处理。")
    guide_label = "目录说明" if preparation_error is None else "说明目标（本次未准备，可能不存在）"
    print(f"{guide_label}：{root / MANAGED_DIRECTORY / GUIDE_FILENAME}")
    if open_directory and os.name == "nt":
        # Shell-open exactly the checked directory, never a material or script.
        _check_directory_chain(root)
        try:
            os.startfile(str(root))
        except OSError as error:
            print(f"目录打开失败：{type(error).__name__}；请按上面的绝对路径手动打开。")
    return root
