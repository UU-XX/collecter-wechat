# -*- coding: utf-8 -*-
"""解析消息资源中的有界摘要，仅供本机视频和文件关联使用。"""

import re

HEX_DIGEST = re.compile(rb"[0-9a-fA-F]{32}")


def resource_digests(blob):
    """只读取有界的二进制字段，不把任意字符串或文件排列猜成资源关联。"""
    found = set()
    if not isinstance(blob, bytes) or len(blob) > 1024 * 1024:
        return found

    def integer(data, position):
        """读取最长十字节的变长整数，截断或溢出立即拒绝。"""
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("资源字段截断")
            part = data[position]
            position += 1
            value |= (part & 127) << shift
            if not part & 128:
                return value, position
        raise ValueError("资源字段过长")

    def walk(data, depth):
        """递归层数和总输入均有界；仅接受完整字段中的摘要。"""
        position, values = 0, set()
        while position < len(data):
            tag, position = integer(data, position)
            wire = tag & 7
            if tag >> 3 == 0:
                raise ValueError("资源字段编号无效")
            if wire == 0:
                _, position = integer(data, position)
            elif wire in (1, 5):
                position += 8 if wire == 1 else 4
            elif wire == 2:
                length, position = integer(data, position)
                end = position + length
                if end > len(data):
                    raise ValueError("资源字段长度无效")
                value = data[position:end]
                position = end
                if HEX_DIGEST.fullmatch(value):
                    values.add(value.decode("ascii").lower())
                elif depth < 5:
                    try:
                        values.update(walk(value, depth + 1))
                    except ValueError:
                        pass
            else:
                raise ValueError("资源编码尚未支持")
            if position > len(data):
                raise ValueError("资源字段截断")
        return values

    try:
        found = walk(blob, 0)
    except ValueError:
        pass
    return found
