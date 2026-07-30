"""ファイル読み取り能力を、ハーネスごとの届け方の違いを吸収して配る。

同じ「ファイルを読める」を実現する経路がハーネスで3通りに分かれている。

- **claude**: 組み込み `Read`。`--allowedTools` に載せる。
  ⚠️ `--allowedTools` は**利用可能ツールのホワイトリストではなく事前承認リスト**なので、
  載せなくても組み込みツールは使える（2026-07-24 に `Bash` で実証され、2.0.3 で
  `--disallowedTools Bash` を足した）。よってここに `Read` を載せるのは能力の付与ではなく、
  **黙って使える状態を明示する**ためのもの。
- **agy**: native `read_file`。`invoke-agent.sh` が `--allowed-builtins Read` を見て
  `config.json` の `globalPermissionGrants` へ `read_file(*)` を書く。
- **codex**: 組み込みのファイル読み取りが無く、`--disable shell_tool` でシェルも塞いである。
  代わりに `files` MCP を繋ぐ。`--allowed-builtins Read` は codex では何もしない
  （`invoke-agent.sh` は `WebSearch` しか写像していない）。

**なぜ loop にも配るのか**: 以前は chat だけがこれを要求し、loop はどのハーネスでも
要求していなかった。しかし claude では上記のとおり `Read` が事前承認リストに関係なく
使えるため、**実態としては loop でも読めていた**。agy では chat で書かれた grant が
残留するため、chat を1回でも通ると loop でも読めていた。つまり「loop では読めない」が
成立していたのは codex だけで、3ハーネスで挙動がばらばらだった。
これを揃える（メンテナ判断・2026-07-29）。

⚠️ **読める範囲の制限（denylist）は別作業**。現状は read-anything。
`secrets.yaml` / `.storage` / `.ssh` / `/data/options.json` / 各ハーネスの認証・config home /
`*.pem` などを塞ぐ計画は決定済みだが未実装で、claude 組み込み `Read` と agy native
`read_file` にパス denylist を掛けられるのかという実現性の調査から始まる。
"""

# codex だけが「組み込みのファイル読み取りを持たない」。ここを増やすときは
# そのハーネスに native の読み取り手段が本当に無いかを確認すること。
FILES_MCP_HARNESSES = frozenset({"codex"})

# files MCP の読み取りツール。MCP 名前空間なので claude/agy の native とは別物。
FILES_MCP_READ_TOOL = "mcp__files__read_file"

# claude の組み込み、および agy の native read_file を引き出すための intent 名。
READ_BUILTIN = "Read"


def _has_item(csv: str, item: str) -> bool:
    """CSV の要素として厳密に含まれるか（部分一致で誤判定しない）。"""
    return item in [part.strip() for part in csv.split(",") if part.strip()]


def grant_file_read(allowed_tools: str, mcp_servers, harness: str) -> tuple[str, tuple[str, ...]]:
    """allowed_tools / mcp_servers にファイル読み取り能力を足して返す。

    既に足りているものは足さない（呼び出し側が二重に呼んでも結果が変わらない）。
    """
    allowed = allowed_tools
    servers = tuple(mcp_servers)
    if not _has_item(allowed, READ_BUILTIN):
        allowed = f"{allowed},{READ_BUILTIN}" if allowed else READ_BUILTIN
    if (harness or "").strip() in FILES_MCP_HARNESSES:
        if not _has_item(allowed, FILES_MCP_READ_TOOL):
            allowed = f"{allowed},{FILES_MCP_READ_TOOL}"
        if "files" not in servers:
            # 既定の Codex モデルは大量の tool schema を選別するため、末尾へ足すと
            # read_file だけがモデルから見えなくなる。native Read の代替は基本能力なので
            # 先頭に置き、tool 選別時にも必ず残す（chat 側と同じ理由・2026-07-23）。
            servers = ("files",) + servers
    return allowed, servers
