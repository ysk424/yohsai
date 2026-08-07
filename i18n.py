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
        ("*", "Select Lock"): "選択固定",
        ("Operator", "Select Lock"): "選択固定",
        ("*", "Existing Lock"): "既存固定",
        ("Operator", "Existing Lock"): "既存固定",
        ("Operator", "Load"): "読み込み",
        ("*", "Load"): "読み込み",
        ("Operator", "Zero GRAVITY"): "無重力着付",
        ("*", "Zero GRAVITY"): "無重力着付",
        ("Operator", "Prepare for ZOZO"): "ZOZO用準備作業",
        ("*", "Prepare for ZOZO"): "ZOZO用準備作業",
        ("*", "Body export height"): "ボディ書き出し高さ",
        ("*", "Bottom"): "下",
        ("*", "Top"): "上",
        ("*", "Bottom (cm)"): "下 (cm)",
        ("*", "Top (cm)"): "上 (cm)",
        ("*", "Shell-isect vs Body"): "ボディとの交差検査",
    }
}
