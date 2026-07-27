# SPDX-License-Identifier: GPL-3.0-or-later
"""Japanese interface translations for the Yohsai N-panel."""

from __future__ import annotations


# Each key is a (context, source string) pair. Blender resolves an operator
# button label in the "Operator" context and a panel heading, property name, or
# plain label in the default "*" context, so a string used as both is registered
# under both.
translations_dict = {
    "ja_JP": {
        ("*", "Yohsai"): "洋裁",
        ("*", "Inputs"): "入力",
        ("*", "Pattern Path"): "型紙",
        ("*", "Clothes"): "衣服",
        ("*", "Body"): "ボディ",
        ("*", "Lock"): "ロック",
        ("Operator", "Load"): "読み込み",
        ("*", "Load"): "読み込み",
        ("Operator", "Auto"): "自動",
        ("*", "Auto"): "自動",
        ("Operator", "Zero GRAVITY"): "無重力着付",
        ("*", "Zero GRAVITY"): "無重力着付",
        ("Operator", "Normal GRAVITY"): "着付重力有",
        ("*", "Normal GRAVITY"): "着付重力有",
        ("Operator", "Finished Garment"): "完成メッシュ作成",
        ("*", "Finished Garment"): "完成メッシュ作成",
        ("Operator", "Prepare for ZOZO"): "ZOZO用に書き出す",
        ("*", "Prepare for ZOZO"): "ZOZO用に書き出す",
    }
}
