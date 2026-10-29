"""单篇数据管理：只删除项目副本，操作前备份，图片清理保留恢复副本。"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

from deepseek_analysis import STATUS_LABELS
from image_import import MAX_IMAGE_BYTES, image_store_lock, now
from wechat import DB_PATH


HASH = re.compile(r"[0-9a-f]{64}")
IMAGE_NAME = re.compile(r"0[1-6]_([0-9a-f]{64})\.(png|jpg)")


class ManagementError(ValueError):
    pass


def connect_db(db_path, writable=False):
    path = Path(db_path).resolve()
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                                 uri=True, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def plain_directory(path, create=False):
    if create:
        path.mkdir(exist_ok=True)
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ManagementError(f"拒绝使用链接、联接点或非目录：{path}")


def checked_image(db_path, stored_path, expected_sha=None):
    """拒绝绝对/越界路径、符号链接、Windows 联接点和非本项目命名。"""
    parts = PurePosixPath(str(stored_path).replace("\\", "/")).parts
    if (len(parts) != 3 or parts[0] != "image_store" or not HASH.fullmatch(parts[1])
            or not IMAGE_NAME.fullmatch(parts[2])):
        raise ManagementError("路径不是本项目管理的 image_store 图片副本，已保留。")
    name_sha = IMAGE_NAME.fullmatch(parts[2]).group(1)
    if expected_sha is not None and expected_sha != name_sha:
        raise ManagementError("数据库哈希与副本名称不一致，已保留。")
    base = Path(db_path).resolve().parent
    root = base / "image_store"
    plain_directory(root)
    plain_directory(root / parts[1])
    target = root / parts[1] / parts[2]
    info = target.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ManagementError("副本为链接、共享硬链接或非普通文件，已保留。")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ManagementError("副本超出 image_store，已保留。")
    with target.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != name_sha:
        raise ManagementError("副本大小或哈希已改变，已保留供人工检查。")
    return target, data, info


def reference_keys(connection, db_path):
    base = Path(db_path).resolve().parent
    keys = set()
    for row in connection.execute("SELECT stored_path FROM article_images"):
        # 所有数据库引用都保护，包括不同大小写、绝对路径等旧写法。
        path = base / row[0]
        keys.add(os.path.normcase(str(path.resolve())))
    return keys


def snapshot(connection, article_id):
    if type(article_id) is not int or not 1 <= article_id <= 2**63 - 1:
        raise ManagementError("请输入一个有效的文章 ID。")
    row = connection.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ManagementError("没有找到该文章 ID，没有删除任何内容。")
    return {
        "article": dict(row),
        "images": [dict(r) for r in connection.execute(
            "SELECT * FROM article_images WHERE article_id = ? ORDER BY id", (article_id,))],
        "analyses": [dict(r) for r in connection.execute(
            "SELECT * FROM ai_analyses WHERE article_id = ? ORDER BY id", (article_id,))],
        "image_article": [dict(r) for r in connection.execute(
            "SELECT * FROM image_articles WHERE article_id = ?", (article_id,))],
    }


def read_snapshot(article_id, db_path=DB_PATH):
    with closing(connect_db(db_path)) as connection:
        connection.execute("BEGIN")
        return snapshot(connection, article_id)


def display_title(article, analyses):
    for analysis in reversed(analyses):
        if analysis.get("result_json"):
            try:
                title = json.loads(analysis["result_json"]).get("article_title")
                if isinstance(title, str) and title.strip():
                    return title
            except (ValueError, AttributeError):
                pass
    return article["title"]


def list_articles(db_path=DB_PATH, offset=0):
    with closing(connect_db(db_path)) as connection:
        connection.execute("BEGIN")
        rows = connection.execute("SELECT id FROM articles ORDER BY id DESC LIMIT ? OFFSET ?", (20, offset)).fetchall()
        result = []
        for row in rows:
            item = snapshot(connection, row[0])
            result.append({"id": row[0], "title": display_title(item["article"], item["analyses"]),
                           "imported_at": item["article"]["imported_at"],
                           "status": item["analyses"][-1]["status"] if item["analyses"] else "no_analysis",
                           "image_count": len(item["images"]), "analysis_count": len(item["analyses"])})
        return result


def backup_database(db_path, operation):
    try:
        return _backup_database(db_path, operation)
    except (OSError, sqlite3.Error) as exc:
        raise ManagementError(f"备份失败，没有执行删除或清理，原数据保留。原因：{exc}") from exc


def _backup_database(db_path, operation):
    base = Path(db_path).resolve().parent
    backups = base / "backups"
    plain_directory(backups, create=True)
    name = operation + "_" + now().replace(":", "").replace("+", "_") + "_" + uuid.uuid4().hex
    folder = backups / name
    folder.mkdir(exist_ok=False)
    target = folder / "database.db"
    with closing(connect_db(db_path)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ManagementError(f"数据库备份校验失败，未执行删除。请检查：{folder}")
    return folder, target


def write_journal(path, report):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def clean_candidates(connection, db_path, candidates, folder, report, journal):
    """调用方持有图片锁和数据库写锁；每个文件先备份再单独 unlink。"""
    references = reference_keys(connection, db_path)
    for item in candidates:
        if item["state"] != "planned":
            continue
        phase = "check"
        try:
            target, data, before = checked_image(db_path, item["stored_path"], item["sha256"])
            if os.path.normcase(str(target.resolve())) in references:
                item["state"] = "shared_kept"
            else:
                phase = "backup"
                recovery = folder / "images"
                plain_directory(recovery, create=True)
                # 备份名由程序产生，不采用数据库中的任意路径。
                recovery = recovery / (uuid.uuid4().hex + "_" + target.name)
                item["recovery_copy"] = str(recovery)
                write_journal(journal, report)
                with recovery.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                target, _, current = checked_image(db_path, item["stored_path"], item["sha256"])
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                    raise ManagementError("图片在操作期间发生改变，已保留原位置文件。")
                target.unlink()  # 仅删除逐项验证过的普通图片文件，不递归删除目录。
                item["state"] = "removed_with_backup"
        except FileNotFoundError as exc:
            if phase == "check":
                item["state"] = "missing"
            else:
                item["state"], item["error"] = "kept_error", str(exc)
        except (OSError, ValueError) as exc:
            item["state"], item["error"] = "kept_error", str(exc)
        # 写清单失败时立即停止，剩余文件不再处理；已有恢复副本和初始清单仍保留。
        write_journal(journal, report)


def delete_article(article_id, *, db_path=DB_PATH, delete_images=False, confirmation="", expected=None):
    if confirmation != "DELETE":
        raise ManagementError("必须输入大写 DELETE 确认，未删除。")
    with image_store_lock(db_path):
        initial = read_snapshot(article_id, db_path)
        if expected is not None and initial != expected:
            raise ManagementError("文章或分析记录在确认期间已变化，请重新查看后操作。")
        if any(row["status"] == "running" for row in initial["analyses"]):
            raise ManagementError("该文章有进行中或中断未核实的分析，请先核实调用状态后再删除。")
        folder, backup = backup_database(db_path, "delete_article")
        journal = folder / "operation.json"
        candidates = []
        seen = set()
        for image in initial["images"]:
            if image["stored_path"] not in seen:
                candidates.append({"stored_path": image["stored_path"], "sha256": image["sha256"],
                                   "state": "planned" if delete_images else "kept_by_choice"})
                seen.add(image["stored_path"])
        report = {"operation": "delete_one_article", "created_at": now(), "article_id": article_id,
                  "title": display_title(initial["article"], initial["analyses"]),
                  "database_backup": str(backup), "database_state": "not_deleted", "files": candidates}
        write_journal(journal, report)
        try:
            with closing(connect_db(db_path, writable=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if snapshot(connection, article_id) != initial:
                        raise ManagementError("文章在备份后发生变化，未执行删除，请重新确认。")
                    with closing(connect_db(backup)) as saved:
                        if snapshot(saved, article_id) != initial:
                            raise ManagementError("备份与当前文章不一致，未执行删除。")
                    if connection.execute("PRAGMA foreign_key_check").fetchone():
                        raise ManagementError("数据库已有无效关联，未执行删除，请先检查。")
                    for table in ("ai_analyses", "article_images", "image_articles"):
                        connection.execute("DELETE FROM " + table + " WHERE article_id = ?", (article_id,))
                    connection.execute("DELETE FROM articles WHERE id = ?", (article_id,))
                    if connection.execute("PRAGMA foreign_key_check").fetchone():
                        raise ManagementError("关联关系校验失败。")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except (sqlite3.Error, ValueError, OSError) as exc:
            report["database_state"], report["error"] = "not_deleted", str(exc)
            write_journal(journal, report)
            raise ManagementError(f"数据库删除失败并已回滚，未继续删除图片。备份与清单：{folder}。原因：{exc}") from exc
        report["database_state"] = "deleted"
        try:
            write_journal(journal, report)
            if delete_images:
                with closing(connect_db(db_path, writable=True)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        clean_candidates(connection, db_path, candidates, folder, report, journal)
                    finally:
                        connection.rollback()  # 本阶段只读引用，不修改数据库。
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ManagementError(f"数据库记录已删除，但图片清理未完成。请勿盲目重试；可用备份和清单恢复：{folder}。原因：{exc}") from exc
        return report, journal


def find_orphans(db_path=DB_PATH):
    base = Path(db_path).resolve().parent
    root = base / "image_store"
    if not root.exists() and not root.is_symlink():
        return [], []
    plain_directory(root)
    with closing(connect_db(db_path)) as connection:
        references = reference_keys(connection, db_path)
    candidates, warnings = [], []
    for directory in root.iterdir():
        try:
            if not HASH.fullmatch(directory.name):
                warnings.append(f"保留非项目命名的条目：{directory.name}")
                continue
            plain_directory(directory)
            for target in directory.iterdir():
                relative = target.relative_to(base).as_posix()
                try:
                    checked, _, _ = checked_image(db_path, relative)
                    if os.path.normcase(str(checked.resolve())) not in references:
                        candidates.append({"stored_path": relative,
                                           "sha256": IMAGE_NAME.fullmatch(target.name).group(1), "state": "planned"})
                except (OSError, ValueError) as exc:
                    warnings.append(f"保留 {relative}：{exc}")
        except (OSError, ValueError) as exc:
            warnings.append(f"保留 {directory.name}：{exc}")
    return candidates, warnings


def cleanup_orphans(*, db_path=DB_PATH, confirmation="", expected=None):
    if confirmation != "DELETE":
        raise ManagementError("必须输入大写 DELETE 确认，未清理。")
    with image_store_lock(db_path):
        current, warnings = find_orphans(db_path)
        if expected is None:
            expected = current
        # 仅处理确认前展示过的项；每次删除前仍会再次校验引用与内容。
        candidates = [{"stored_path": row["stored_path"], "sha256": row["sha256"], "state": "planned"}
                      for row in expected]
        if not candidates:
            return {"operation": "cleanup_orphans", "files": [], "warnings": warnings}, None
        folder, backup = backup_database(db_path, "cleanup_orphans")
        journal = folder / "operation.json"
        report = {"operation": "cleanup_orphans", "created_at": now(), "database_backup": str(backup),
                  "database_state": "unchanged", "files": candidates, "warnings": warnings}
        write_journal(journal, report)
        try:
            with closing(connect_db(db_path, writable=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    clean_candidates(connection, db_path, candidates, folder, report, journal)
                finally:
                    connection.rollback()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise ManagementError(f"孤立图片清理未完成；数据库未删除记录，剩余文件保留。恢复副本和清单：{folder}。原因：{exc}") from exc
        return report, journal


def tree_size(root):
    """统计文件大小，不跟随符号链接或 Windows 联接点。"""
    total = files = skipped = 0
    if not root.exists() and not root.is_symlink():
        return {"bytes": 0, "files": 0, "skipped": 0}
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
            if is_link(info):
                skipped += 1
            elif stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                files += 1
        except OSError:
            skipped += 1
    return {"bytes": total, "files": files, "skipped": skipped}


def disk_usage(db_path=DB_PATH):
    path = Path(db_path).resolve()
    db_bytes = 0
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            info = candidate.lstat()
            if not is_link(info) and stat.S_ISREG(info.st_mode):
                db_bytes += info.st_size
    return {"database_bytes": db_bytes, "image_store": tree_size(path.parent / "image_store"),
            "backups": tree_size(path.parent / "backups")}


def print_result(report, journal):
    states = {}
    for item in report["files"]:
        states[item["state"]] = states.get(item["state"], 0) + 1
    print(f"图片处理：移出并留有备份 {states.get('removed_with_backup', 0)} 张，"
          f"共享引用保留 {states.get('shared_kept', 0)} 张，"
          f"按选择保留 {states.get('kept_by_choice', 0)} 张，"
          f"本已缺失 {states.get('missing', 0)} 张，异常保留 {states.get('kept_error', 0)} 张。")
    for item in report["files"]:
        if item.get("error"):
            print(f"未删除 {item['stored_path']}：{item['error']}")
    if journal:
        print(f"数据库备份、图片恢复副本及操作清单：{journal.parent}")


def input_id():
    value = input("请输入单篇文章 ID（回车取消）：").strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise ManagementError("请输入一个正整数文章 ID。")
    return int(value)


def browse_data(db_path):
    offset = 0
    while True:
        rows = list_articles(db_path, offset)
        if not rows:
            print("没有更多文章。")
            return
        for row in rows:
            status = STATUS_LABELS.get(row["status"], "无分析" if row["status"] == "no_analysis" else row["status"])
            print(f"ID {row['id']} | {row['title']}\n  导入：{row['imported_at']} | {status} | "
                  f"图片 {row['image_count']} 张 | 分析记录 {row['analysis_count']} 条")
        choice = input("输入文章 ID 查看关联记录，N 下一页，回车返回：").strip()
        if not choice:
            return
        if choice.upper() == "N":
            offset += 20
            continue
        if not choice.isascii() or not choice.isdecimal() or len(choice) > 18:
            print("请输入文章 ID。")
            continue
        item = read_snapshot(int(choice), db_path)
        print(f"原始标题：{item['article']['title']}\n正文：{item['article']['content']}")
        for row in item["analyses"]:
            print(f"分析记录 {row['id']} | {row['model']} | {row['analyzed_at']} | "
                  f"{STATUS_LABELS.get(row['status'], row['status'])}")
            if row["error_message"]:
                print(f"  提示：{row['error_message']}")
        print("结构化结果和原文证据仍可使用主菜单 6 查看。")


def data_management_menu(db_path=DB_PATH):
    while True:
        choice = input("\n数据管理\n1. 查看文章及分析记录\n2. 按文章 ID 删除单篇文章\n"
                       "3. 仅清理孤立图片\n4. 查看空间占用\n0. 返回主菜单\n请选择：").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                browse_data(db_path)
            elif choice == "2":
                article_id = input_id()
                if article_id is None:
                    continue
                expected = read_snapshot(article_id, db_path)
                print(f"即将删除文章 ID {article_id}：{display_title(expected['article'], expected['analyses'])}")
                print(f"关联分析 {len(expected['analyses'])} 条，图片记录 {len(expected['images'])} 条。")
                remove_images = input("同时移除无其他引用的项目图片副本？输入 Y；回车默认保留：").strip().upper() == "Y"
                print("会先自动备份数据库。图片如需移除，也先保存恢复副本到 backups；备份仍占磁盘空间。")
                confirmation = input("输入大写 DELETE 确认删除；其他输入取消：").strip()
                if confirmation != "DELETE":
                    print("已取消，没有删除。")
                    continue
                report, journal = delete_article(article_id, db_path=db_path, delete_images=remove_images,
                                                 confirmation=confirmation, expected=expected)
                print("该文章及关联分析、图片登记记录已删除。")
                print_result(report, journal)
            elif choice == "3":
                candidates, warnings = find_orphans(db_path)
                print(f"找到 {len(candidates)} 张无数据库引用、格式和哈希通过检查的项目副本。")
                for item in candidates[:20]:
                    print("  " + item["stored_path"])
                if len(candidates) > 20:
                    print(f"另有 {len(candidates) - 20} 张；只处理本次扫描的这些孤立副本。")
                if warnings:
                    print(f"另有 {len(warnings)} 项因非项目命名、链接或异常而保留，不会删除。")
                if not candidates:
                    continue
                print("不会删除任何数据库记录。将先备份数据库及待清理图片，备份仍占磁盘空间。")
                confirmation = input("输入大写 DELETE 确认清理；其他输入取消：").strip()
                if confirmation != "DELETE":
                    print("已取消，没有清理。")
                    continue
                report, journal = cleanup_orphans(db_path=db_path, confirmation=confirmation, expected=candidates)
                print_result(report, journal)
            elif choice == "4":
                usage = disk_usage(db_path)
                print(f"数据库（含 WAL/SHM）：{usage['database_bytes']:,} 字节（{usage['database_bytes']/1024**2:.2f} MiB）")
                for key, label in (("image_store", "图片目录"), ("backups", "备份和恢复副本目录")):
                    item = usage[key]
                    print(f"{label}：{item['bytes']:,} 字节（{item['bytes']/1024**2:.2f} MiB），"
                          f"{item['files']} 个文件，跳过链接或不可读条目 {item['skipped']} 项。")
                print("以上为文件大小合计，不是 NTFS 实际分配空间；不自动压缩数据库或清空备份。")
            else:
                print("请输入 0 到 4。")
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"数据管理操作未完整完成：{exc}")
