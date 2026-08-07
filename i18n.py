# SPDX-License-Identifier: GPL-3.0-or-later
"""Japanese interface translations for the Yohsai N-panel.

UI labels stay English as Blender identifiers and are translated through
``translations_dict`` when the interface language is Japanese.

Dynamic status / error strings (the Message box) use :func:`msg` so they can
carry format arguments and still follow the same language choice.
"""

from __future__ import annotations


# Each key is a (context, source string) pair. Blender resolves an operator
# button label in the "Operator" context and a panel heading, property name, or
# plain label in the default "*" context, so a string used as both is registered
# under both.
translations_dict = {
    "ja_JP": {
        ("*", "Yohsai"): "洋裁",
        ("*", "Inputs"): "入力",
        ("*", "Message"): "メッセージ",
        ("*", "Pattern Path"): "型紙",
        ("*", "Clothes"): "衣服",
        ("*", "Body"): "ボディ",
        ("*", "Status"): "状態",
        ("*", "Select Lock"): "選択固定",
        ("Operator", "Select Lock"): "選択固定",
        ("*", "Existing Lock"): "既存固定",
        ("Operator", "Existing Lock"): "既存固定",
        ("Operator", "Load"): "読み込み",
        ("*", "Load"): "読み込み",
        ("Operator", "Update"): "更新",
        ("*", "Update"): "更新",
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
        ("*", "ZOZO Contact Solver"): "ZOZO Contact Solver",
        ("*", "Ready"): "準備完了",
        ("*", "Loading..."): "読み込み中...",
        ("*", "Updating..."): "更新中...",
    }
}


def is_japanese() -> bool:
    """True when Blender's UI language is Japanese."""
    try:
        import bpy

        locale = str(getattr(bpy.app.translations, "locale", "") or "")
        return locale.lower().startswith("ja")
    except Exception:
        return False


# Dynamic status / soft-error templates. Keys are stable English ids.
_STATUS_EN: dict[str, str] = {
    "ready": "Ready",
    "loading": "Loading...",
    "updating": "Updating...",
    "select_lock_need_parts": "Select clothes part(s) before Select Lock.",
    "select_lock_on_hint": "Select Lock on: select clothes part(s) to lock.",
    "select_lock_off": "Select Lock off.",
    "select_lock_on_locked": "Select Lock on: locked {n} selected part(s).",
    "select_lock_off_unlocked": "Select Lock off: unlocked {n} selected part(s).",
    "select_lock_on_locked_update": "Select Lock on: locked {n} selected clothes part(s).",
    "load_already": "A pattern is already being loaded.",
    "load_need_pdf": "Select a PDF pattern file first.",
    "load_need_pdf_file": "Pattern Path must point to an existing .pdf file.",
    "parser_missing": "Parser program is missing: {name}",
    "parser_start_failed": "Could not start pattern parser: {exc}",
    "update_already": "A pattern is already being processed.",
    "update_need_clothes": "Select a loaded Clothes collection before Update.",
    "update_need_pdf": "Select the original PDF file first.",
    "update_need_pdf_file": "Pattern Path must point to the existing source .pdf file.",
    "update_same_pdf": (
        "Update must use the same pattern file that created the selected Clothes collection."
    ),
    "load_failed": "Load failed: {exc}",
    "update_failed": "Update failed: {exc}",
    "loaded_parts": "Loaded {name}: {n} part(s); Auto lock on",
    "updated_mesh": "Updated {name}: {n} vertices",
    "updated_sewing_rebuild": "; Sewing will rebuild on Zero GRAVITY",
    "zero_g_failed": "Zero GRAVITY failed: {exc}",
    "zero_g_need_parts": "Move at least two connected pattern parts before pressing GRAVITY.",
    "zero_g_need_clothes": "No loaded Yohsai clothes collection is selected.",
    "zero_g_need_body": "Select a mesh Body before pressing Zero GRAVITY.",
    "prepare_mcp_running": "ZOZO MCP configuration is already running.",
    "prepare_stopped": "Prepare for ZOZO stopped: {message}{suffix}",
    "prepare_failed": "Prepare for ZOZO failed: {message}{suffix}",
    "prepare_summary": (
        "{recut}prepared {seams} ZOZO stitches "
        "(widest seam still open {gap_mm:.2f} mm){shell}{quality}"
    ),
    "prepare_recut": "re-cut {n} panel(s); ",
    "prepare_mcp_configuring": "{summary}; {mcp_note}; configuring ZOZO MCP on :{port}...",
    "prepare_mcp_start_fail": (
        "{summary}; copies are ready, but MCP could not start: {exc}"
    ),
    "mcp_started": "MCP started on :{port}",
    "mcp_start_fail": (
        "Could not start ZOZO MCP on :{port} ({detail}). "
        "Enable ZOZO Contact Solver and use MCP Start, then Prepare again."
    ),
    "mcp_setup_failed": "{summary}; ZOZO MCP setup failed: {detail}",
    "mcp_ready": (
        "{summary}; ZOZO MCP ready ({capture}){conn}. "
        "Use Transfer, then Run Simulation."
    ),
    "mcp_response_failed": "{summary}; ZOZO MCP response failed: {detail}",
    "prepared_default": "Prepared the ZOZO hand-off mesh",
    "shell_unavailable": "shell-isect unavailable",
    "shell_suffix": " [shell-isect {ver}]",
    "shell_suffix_missing": " [shell-isect unavailable]",
    # shell-isect / quality (user-facing status box)
    "shell_err_unavailable": (
        "ERROR: self-intersection check unavailable ({message}) [{suffix}]"
    ),
    "shell_err_failed": (
        "ERROR: self-intersection check failed ({message}) [{suffix}]"
    ),
    "shell_err_pairs": (
        "ERROR: self-intersection (tri-tri face pairs): {pipeline}"
        "{faces}{pairs} [{suffix}]"
    ),
    "shell_faces_range": " cloth_faces=0..{last}",
    "shell_face_pairs": " face_pairs: {pairs}",
    "shell_mode_both": "cloth+body",
    "shell_mode_cloth": "cloth-only",
    "shell_summary": "shell-isect {version} ({mode}{crop}): {pipeline}",
    "shell_crop": ", body {tested}/{total} tris",
    "shell_pipeline_clean": "check1=0 (clean; fix skipped)",
    "shell_pipeline": "check1={before} fix={fix} check2={after}",
    "quality_summary": (
        "triangle quality: {faces} faces, smallest rest area "
        "{area_min:.2e} m² (floor {floor:.2e}), "
        "shortest edge {edge_mm:.3f} mm, "
        "worst aspect {aspect:.2e}, "
        "{failing} under the floor"
    ),
    "quality_error": (
        "ERROR: {failing} triangle(s) have too little rest area for the solver: "
        "smallest {area_min:.2e} m² against a floor of {floor:.2e} m². "
        "A shell element's stiffness scales with 1/area, so these take the first "
        "solve to NaN and it stops after frame 0"
    ),
    "quality_worst": ". Worst: {shown}",
    "quality_worst_more": ", ... (+{n} more)",
    "quality_worst_item": (
        "(face {index}: area {area:.2e} m², shortest edge {edge_mm:.4f} mm)"
    ),
    # zozo handoff hard errors
    "zozo_need_clothes": "Select a loaded Yohsai Clothes collection first.",
    "zozo_need_body": "Select a mesh Body before Prepare for ZOZO.",
    "zozo_no_seams": "The garment has no sewing edges.",
    "zozo_nonfinite": "The cloth contains a non-finite vertex position.",
    "zozo_seam_mismatch": "The sewing pairs do not match the current panel vertices.",
    "zozo_topo_changed": "The ZOZO hand-off topology changed while creating the mesh.",
    "zozo_stitch_lost": "A loose ZOZO stitch edge was lost while creating the mesh.",
    "zozo_no_body_export": "ZOZO body was not exported; cannot configure MCP.",
    "zozo_stale_pitch": (
        "{n} panel(s) were cut on a lattice this build no longer cuts and would "
        "go over at the wrong pitch: {shown}, against {mm:.0f} mm. Press Update "
        "to re-cut the pattern, then run GRAVITY again before handing over"
    ),
    "zozo_pattern_missing": (
        "{name} has no valid Yohsai pattern coordinates; load the pattern again."
    ),
}

_STATUS_JA: dict[str, str] = {
    "ready": "準備完了",
    "loading": "読み込み中...",
    "updating": "更新中...",
    "select_lock_need_parts": "選択固定の前に衣服パーツを選択してください。",
    "select_lock_on_hint": "選択固定オン: 固定する衣服パーツを選択してください。",
    "select_lock_off": "選択固定オフ。",
    "select_lock_on_locked": "選択固定オン: 選択中の {n} パーツを固定しました。",
    "select_lock_off_unlocked": "選択固定オフ: 選択中の {n} パーツの固定を解除しました。",
    "select_lock_on_locked_update": "選択固定オン: 選択中の衣服パーツ {n} 個を固定しました。",
    "load_already": "別の型紙を読み込み中です。",
    "load_need_pdf": "先に PDF 型紙ファイルを指定してください。",
    "load_need_pdf_file": "型紙パスは存在する .pdf ファイルを指す必要があります。",
    "parser_missing": "パーサが見つかりません: {name}",
    "parser_start_failed": "型紙パーサを起動できませんでした: {exc}",
    "update_already": "別の型紙を処理中です。",
    "update_need_clothes": "更新の前に読み込み済みの衣服コレクションを選択してください。",
    "update_need_pdf": "元の PDF ファイルを先に指定してください。",
    "update_need_pdf_file": "型紙パスは元の .pdf ファイルを指す必要があります。",
    "update_same_pdf": (
        "更新には、選択中の衣服コレクションを作ったときと同じ型紙ファイルが必要です。"
    ),
    "load_failed": "読み込み失敗: {exc}",
    "update_failed": "更新失敗: {exc}",
    "loaded_parts": "読み込み完了 {name}: {n} パーツ; 既存固定オン",
    "updated_mesh": "更新完了 {name}: 頂点 {n}",
    "updated_sewing_rebuild": "; 無重力着付時に縫い目を再構築します",
    "zero_g_failed": "無重力着付 失敗: {exc}",
    "zero_g_need_parts": "着付の前に、つながる型紙パーツを少なくとも 2 つ配置してください。",
    "zero_g_need_clothes": "読み込み済みの衣服コレクションが選択されていません。",
    "zero_g_need_body": "無重力着付の前にメッシュのボディを選択してください。",
    "prepare_mcp_running": "ZOZO MCP の設定がすでに実行中です。",
    "prepare_stopped": "ZOZO用準備作業 中断: {message}{suffix}",
    "prepare_failed": "ZOZO用準備作業 失敗: {message}{suffix}",
    "prepare_summary": (
        "{recut}ZOZO ステッチ {seams} 本を準備 "
        "(最も開いている縫い目 {gap_mm:.2f} mm){shell}{quality}"
    ),
    "prepare_recut": "パネル {n} 枚を再カット; ",
    "prepare_mcp_configuring": "{summary}; {mcp_note}; ZOZO MCP を :{port} で設定中...",
    "prepare_mcp_start_fail": (
        "{summary}; コピーはできましたが MCP を開始できませんでした: {exc}"
    ),
    "mcp_started": "MCP を :{port} で開始しました",
    "mcp_start_fail": (
        "ZOZO MCP を :{port} で開始できませんでした ({detail})。"
        "ZOZO Contact Solver を有効にし MCP Start してから、もう一度準備してください。"
    ),
    "mcp_setup_failed": "{summary}; ZOZO MCP 設定失敗: {detail}",
    "mcp_ready": (
        "{summary}; ZOZO MCP 準備完了 ({capture}){conn}。"
        "Transfer のあと Run Simulation を実行してください。"
    ),
    "mcp_response_failed": "{summary}; ZOZO MCP 応答失敗: {detail}",
    "prepared_default": "ZOZO 引き渡しメッシュを準備しました",
    "shell_unavailable": "shell-isect 利用不可",
    "shell_suffix": " [shell-isect {ver}]",
    "shell_suffix_missing": " [shell-isect 利用不可]",
    "shell_err_unavailable": (
        "エラー: 自己交差チェックを利用できません ({message}) [{suffix}]"
    ),
    "shell_err_failed": (
        "エラー: 自己交差チェックに失敗しました ({message}) [{suffix}]"
    ),
    "shell_err_pairs": (
        "エラー: 自己交差 (三角×三角の面ペア): {pipeline}"
        "{faces}{pairs} [{suffix}]"
    ),
    "shell_faces_range": " 布面=0..{last}",
    "shell_face_pairs": " 面ペア: {pairs}",
    "shell_mode_both": "布+ボディ",
    "shell_mode_cloth": "布のみ",
    "shell_summary": "shell-isect {version} ({mode}{crop}): {pipeline}",
    "shell_crop": ", ボディ {tested}/{total} 三角",
    "shell_pipeline_clean": "check1=0 (クリーン; 修正スキップ)",
    "shell_pipeline": "check1={before} fix={fix} check2={after}",
    "quality_summary": (
        "三角品質: {faces} 面, 最小面積 "
        "{area_min:.2e} m² (下限 {floor:.2e}), "
        "最短辺 {edge_mm:.3f} mm, "
        "最悪アスペクト {aspect:.2e}, "
        "下限未満 {failing}"
    ),
    "quality_error": (
        "エラー: ソルバに渡せないほど面積が小さい三角が {failing} 枚あります。"
        "最小 {area_min:.2e} m² (下限 {floor:.2e} m²)。"
        "シェル要素の剛性は 1/面積 に比例するため、これらは最初の求解で NaN になり"
        "フレーム 0 で止まります"
    ),
    "quality_worst": "。最悪: {shown}",
    "quality_worst_more": ", ... (他 {n} 件)",
    "quality_worst_item": (
        "(面 {index}: 面積 {area:.2e} m², 最短辺 {edge_mm:.4f} mm)"
    ),
    "zozo_need_clothes": "先に読み込み済みの衣服コレクションを選択してください。",
    "zozo_need_body": "ZOZO用準備作業の前にメッシュのボディを選択してください。",
    "zozo_no_seams": "衣服に縫い目がありません。",
    "zozo_nonfinite": "布に有限でない頂点座標があります。",
    "zozo_seam_mismatch": "縫い目ペアが現在のパネル頂点と一致しません。",
    "zozo_topo_changed": "ZOZO 引き渡しメッシュ作成中にトポロジが変わりました。",
    "zozo_stitch_lost": "緩い ZOZO ステッチ辺が作成中に失われました。",
    "zozo_no_body_export": "ZOZO ボディが書き出されていないため MCP を設定できません。",
    "zozo_stale_pitch": (
        "このビルドが使わない格子で切られたパネルが {n} 枚あり、誤ったピッチのまま"
        "渡ってしまいます: {shown} (現在は {mm:.0f} mm)。更新で型紙を再カットし、"
        "着付してから渡してください"
    ),
    "zozo_pattern_missing": (
        "{name} に有効な型紙座標がありません。型紙を読み込み直してください。"
    ),
}


def msg(key: str, **kwargs) -> str:
    """Format a user-facing status / soft-error string in the UI language."""
    table = _STATUS_JA if is_japanese() else _STATUS_EN
    template = table.get(key) or _STATUS_EN.get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template
