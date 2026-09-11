import os
import re
import hashlib


class PathUtils:

    @staticmethod
    def iter_dir_files(in_path, exts="", filesize=0, episode_format=None):
        """
        生成目录下的媒体文件，按后缀、大小、格式过滤。
        """
        if not in_path:
            return
        if not os.path.exists(in_path):
            return
        extensions = {ext.lower() for ext in exts} if not isinstance(exts, str) else None

        def matches(path, filename):
            if PathUtils.is_invalid_path(path):
                return False
            if episode_format and not episode_format.match(filename):
                return False
            suffix = os.path.splitext(filename)[-1].lower()
            if exts and ((suffix not in extensions) if extensions is not None else suffix not in exts):
                return False
            return not filesize or os.path.getsize(path) >= filesize

        if os.path.isdir(in_path):
            for root, dirs, files in os.walk(in_path):
                dirs[:] = [name for name in dirs
                           if not PathUtils.is_invalid_path(os.path.join(root, name))]
                for file in files:
                    cur_path = os.path.join(root, file)
                    if matches(cur_path, file):
                        yield cur_path
        else:
            if matches(in_path, os.path.basename(in_path)):
                yield in_path

    @staticmethod
    def get_dir_files(in_path, exts="", filesize=0, episode_format=None):
        """获得目录下的媒体文件列表，保留旧的列表接口。"""
        return list(PathUtils.iter_dir_files(in_path, exts, filesize, episode_format))

    @staticmethod
    def has_matching_file(in_path, exts="", filesize=0, episode_format=None):
        """只判断是否存在匹配文件，命中后立即停止遍历。"""
        return next(PathUtils.iter_dir_files(in_path, exts, filesize, episode_format), None) is not None

    @staticmethod
    def get_dir_level1_files(in_path, exts=""):
        """
        查询目录下的文件（只查询一级）
        """
        ret_list = []
        if not os.path.exists(in_path):
            return []
        with os.scandir(in_path) as entries:
            for entry in entries:
                if entry.is_file():
                    if not exts or os.path.splitext(entry.name)[-1].lower() in exts:
                        ret_list.append(entry.path)
        return ret_list

    @staticmethod
    def get_dir_level1_medias(in_path, exts=""):
        """
        根据后缀，返回目录下所有的文件及文件夹列表（只查询一级）
        """
        ret_list = []
        if not os.path.exists(in_path):
            return []
        if os.path.isdir(in_path):
            for file in os.listdir(in_path):
                path = os.path.join(in_path, file)
                if os.path.isfile(path):
                    if not exts or os.path.splitext(file)[-1].lower() in exts:
                        ret_list.append(path)
                else:
                    ret_list.append(path)
        else:
            ret_list.append(in_path)
        return ret_list

    @staticmethod
    def is_invalid_path(path):
        """
        判断是否不能处理的路径
        """
        if not path:
            return True
        if path.find('/@Recycle/') != -1 or path.find('/#recycle/') != -1 or path.find('/.') != -1 or path.find(
                '/@eaDir') != -1:
            return True
        return False

    @staticmethod
    def is_path_in_path(path1, path2):
        """
        判断两个路径是否包含关系 path1 in path2
        """
        if not path1 or not path2:
            return False
        path1 = os.path.normpath(path1)
        path2 = os.path.normpath(path2)
        if path1 == path2:
            return True
        path = os.path.dirname(path2)
        while True:
            if path == path1:
                return True
            path = os.path.dirname(path)
            if path == os.path.dirname(path):
                break
        return False

    @staticmethod
    def get_bluray_dir(path):
        """
        判断是否蓝光原盘目录，是则返回原盘的根目录，否则返回空
        """
        if not path or not os.path.exists(path):
            return None
        if os.path.isdir(path):
            if os.path.exists(os.path.join(path, "BDMV", "index.bdmv")):
                return path
            elif os.path.normpath(path).endswith("BDMV") \
                    and os.path.exists(os.path.join(path, "index.bdmv")):
                return os.path.dirname(path)
            elif os.path.normpath(path).endswith("STREAM") \
                    and os.path.exists(os.path.join(os.path.dirname(path), "index.bdmv")):
                return PathUtils.get_parent_paths(path, 2)
            else:
                # 电视剧原盘下会存在多个目录形如：Spider Man 2021/DIsc1, Spider Man 2021/Disc2
                for level1 in PathUtils.get_dir_level1_medias(path):
                    if os.path.exists(os.path.join(level1, "BDMV", "index.bdmv")):
                        return path
                return None
        else:
            if str(os.path.splitext(path)[-1]).lower() in [".m2ts", ".ts"] \
                    and os.path.normpath(os.path.dirname(path)).endswith("STREAM") \
                    and os.path.exists(os.path.join(PathUtils.get_parent_paths(path, 2), "index.bdmv")):
                return PathUtils.get_parent_paths(path, 3)
            else:
                return None

    @staticmethod
    def get_parent_paths(path, level: int = 1):
        """
        获取父目录路径，level为向上查找的层数
        """
        for lv in range(0, level):
            path = os.path.dirname(path)
        return path

    @staticmethod
    def is_valid_filename(filename):
        # 文件名合法性的正则表达式
        valid_characters = r'^[a-zA-Z0-9_\-\.]+$'

        # 使用正则表达式检查文件名是否合法
        if re.match(valid_characters, filename):
            return True
        else:
            return False

    @staticmethod
    def sanitize_filename(filename):
        # 替换不合法字符为下划线
        return re.sub(r'[^\w\.\-]', '_', filename)

    @staticmethod
    def calculate_md5(filename):
        md5_hash = hashlib.md5()
        md5_hash.update(filename.encode('utf-8'))
        return md5_hash.hexdigest()
