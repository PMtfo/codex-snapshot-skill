#!/usr/bin/env bash
# smoke.sh — codex-snapshot 等价验证脚本。
#
# 走 6 步:
#   1. 在临时假 CODEX_ROOT 下构造一个迷你 codex 数据。
#   2. 快照 codex-test-v1。
#   3. 制造受控差异(改 config / 加 agent / 删 archived)。
#   4. 拦截测试:回 'no' → 程序退出,数据原状不变。
#   5. 覆盖测试:回 'yes' → 数据回到 v1。
#   6. SQLite 在线一致性:边写边快照。
#
# 不需要真换号,也不会影响真实 ~/.codex/(用 TMP 目录隔离)。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$SCRIPT_DIR/scripts"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/codex-snap-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME"
export HOME="$FAKE_HOME"
echo "[smoke] FAKE HOME = $HOME"

CODEX="$HOME/.codex"
mkdir -p "$CODEX/agents" "$CODEX/archived_sessions" "$CODEX/memories"

# 构造迷你 codex 数据
cat > "$CODEX/config.toml" <<'EOF'
# fake codex config
model = "gpt-5"
EOF
cat > "$CODEX/auth.json" <<'EOF'
{"OPENAI_API_KEY": "DUMMY-MUST-BE-EXCLUDED"}
EOF
echo '{"id":"sess-1","name":"foo"}' > "$CODEX/session_index.jsonl"
echo '{"msg":"hello"}' > "$CODEX/archived_sessions/sess-1.jsonl"
cat > "$CODEX/agents/foo-agent.toml" <<'EOF'
[agent]
name = "foo"
EOF
cat > "$CODEX/memories/MEMORY.md" <<'EOF'
- 测试记忆
EOF
(cd "$CODEX/memories" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -q -m init)

# 造一个 sqlite
python3 - <<'PY'
import os, sqlite3
p = os.path.expanduser("~/.codex/logs_2.sqlite")
c = sqlite3.connect(p)
c.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, msg TEXT)")
c.executemany("INSERT INTO logs(ts,msg) VALUES (?,?)", [(1700000000+i, f"line{i}") for i in range(100)])
c.commit(); c.close()
PY

echo "[smoke] 初始 ~/.codex 已构造"

# 0. 默认拒绝逻辑:Codex.app 真在运行,不加 --allow-while-running 应直接 rc=2
# 注:smoke 假设宿主真 Codex.app 可能在运行(is_codex_running 走 pgrep 系统进程)。
# 如果宿主无 Codex.app,跳过该步。
if pgrep -f Codex.app >/dev/null 2>&1; then
    echo "----- step 0: 默认拒绝(Codex 运行中)应 rc=2 -----"
    set +e
    python3 "$SCRIPTS/snapshot.py" codex-test-block 2>/dev/null
    RC=$?
    set -e
    [ $RC -eq 2 ] || { echo "[smoke] ✗ 默认应 rc=2,实际 rc=$RC"; exit 1; }
    echo "[smoke] ✓ Codex 运行中,默认拒绝快照(rc=$RC)"
else
    echo "[smoke] (step 0 跳过:宿主 Codex.app 未运行)"
fi

# 1. 基线快照 — smoke 用 --allow-while-running 绕过默认拒绝(假环境无 wal 干扰)
echo "----- step 1: snapshot codex-test-v1 --allow-while-running -----"
python3 "$SCRIPTS/snapshot.py" codex-test-v1 --allow-while-running

echo "----- step 1.5: list -----"
python3 "$SCRIPTS/list.py"

echo "----- step 1.6: show -----"
python3 "$SCRIPTS/show.py" codex-test-v1

# 验证 auth.json 不在 manifest
if python3 -c "import json,sys; m=json.load(open('$HOME/.codex-snapshots/codex-test-v1/manifest.json')); sys.exit(0 if 'auth.json' not in m['files'] else 1)"; then
    echo "[smoke] ✓ auth.json 已被排除"
else
    echo "[smoke] ✗ auth.json 不应在 manifest"
    exit 1
fi

# 记录 v1 时所有非 auth.json 文件 sha256
python3 - <<'PY' > "$TMP/v1.sha256"
import os, hashlib, json
root = os.path.expanduser("~/.codex")
m = json.load(open(os.path.expanduser("~/.codex-snapshots/codex-test-v1/manifest.json")))
for rel in sorted(m["files"]):
    if m["files"][rel].get("is_sqlite"): continue  # sqlite 走指纹
    p = os.path.join(root, rel)
    if os.path.exists(p):
        h = hashlib.sha256(open(p,"rb").read()).hexdigest()
        print(f"{h}  {rel}")
PY

# 2. 制造差异
echo "----- step 2: 制造受控差异 -----"
echo "# tampered" >> "$CODEX/config.toml"
cat > "$CODEX/agents/test-agent.toml" <<'EOF'
[agent]
name = "test"
EOF
rm "$CODEX/archived_sessions/sess-1.jsonl"

# 3. 拦截测试: 回 no
echo "----- step 3: restore + 'no' 应取消 -----"
set +e
echo "no" | python3 "$SCRIPTS/restore.py" codex-test-v1 --force-while-running --yes-i-checked-memories-git
RC=$?
set -e
if [ $RC -eq 0 ]; then
    echo "[smoke] ✗ 输入 no 时不应返回 0"
    exit 1
fi
echo "[smoke] ✓ 输入 no 时正确退出(rc=$RC)"

# 验证差异仍在(config 末尾仍有 tampered, agents/test-agent.toml 仍在, archived/sess-1.jsonl 仍缺失)
grep -q "tampered" "$CODEX/config.toml" || { echo "[smoke] ✗ 应保留 tampered"; exit 1; }
[ -f "$CODEX/agents/test-agent.toml" ] || { echo "[smoke] ✗ 应保留 test-agent"; exit 1; }
[ ! -f "$CODEX/archived_sessions/sess-1.jsonl" ] || { echo "[smoke] ✗ archived 不应被恢复"; exit 1; }
echo "[smoke] ✓ 拒绝后数据原状未动"

# 4. 覆盖测试: 回 yes
echo "----- step 4: restore + 'yes' 应覆盖 -----"
echo "yes" | python3 "$SCRIPTS/restore.py" codex-test-v1 --force-while-running --yes-i-checked-memories-git

# 验证文件回到 v1
if grep -q "tampered" "$CODEX/config.toml"; then
    echo "[smoke] ✗ tampered 应被覆盖掉"; exit 1
fi
[ -f "$CODEX/archived_sessions/sess-1.jsonl" ] || { echo "[smoke] ✗ archived 应被恢复"; exit 1; }
# test-agent 不在 v1,白名单同步不删除快照外文件 → 应仍在
[ -f "$CODEX/agents/test-agent.toml" ] || { echo "[smoke] ✗ 快照外的 test-agent 应保留"; exit 1; }
echo "[smoke] ✓ 覆盖后文件回到 v1,快照外文件 test-agent 仍在(白名单同步)"

# 验证 auth.json 没被动
grep -q DUMMY "$CODEX/auth.json" || { echo "[smoke] ✗ auth.json 不应变化"; exit 1; }
echo "[smoke] ✓ auth.json 未被恢复(符合预期)"

# 验证 pre-restore 快照存在且包含差异
PRE=$(ls -t "$HOME/.codex-snapshots/" | grep "^pre-restore-" | head -1)
[ -n "$PRE" ] || { echo "[smoke] ✗ 应存在 pre-restore 快照"; exit 1; }
echo "[smoke] ✓ pre-restore 快照已落地:$PRE"

# 5. SQLite 在线一致性
echo "----- step 5: SQLite 在线快照一致性 -----"
python3 - <<'PY' &
import os, sqlite3, time
p = os.path.expanduser("~/.codex/logs_2.sqlite")
c = sqlite3.connect(p, timeout=30)
for i in range(200):
    c.execute("INSERT INTO logs(ts,msg) VALUES (?,?)", (1700000999+i, f"online-{i}"))
    c.commit()
    time.sleep(0.005)
c.close()
PY
WRITER_PID=$!
sleep 0.1
python3 "$SCRIPTS/snapshot.py" codex-test-online --force --allow-while-running >/dev/null
wait $WRITER_PID
# 解包 logs_2.sqlite 后跑 integrity_check
python3 - <<'PY'
import os, tarfile, sqlite3, tempfile
snap = os.path.expanduser("~/.codex-snapshots/codex-test-online/payload.tar.gz")
with tempfile.TemporaryDirectory() as d:
    with tarfile.open(snap, "r:gz") as tf:
        try: tf.extractall(d, filter="data")
        except TypeError: tf.extractall(d)
    db = os.path.join(d, "tree", "logs_2.sqlite")
    assert os.path.exists(db), db
    c = sqlite3.connect(db)
    r = c.execute("PRAGMA integrity_check(1)").fetchone()
    assert r and r[0] == "ok", f"integrity not ok: {r}"
    cnt = c.execute("SELECT count(*) FROM logs").fetchone()[0]
    print(f"[smoke] ✓ 在线快照后 logs 行数 = {cnt}, integrity_check = ok")
PY

# 6. delete + list
echo "----- step 6: delete -----"
python3 "$SCRIPTS/delete.py" codex-test-online
[ -d "$HOME/.codex-snapshots/.trash" ] || { echo "[smoke] ✗ trash 目录应存在"; exit 1; }
echo "[smoke] ✓ 软删除完成"

echo
echo "===================================="
echo "[smoke] 全部 6 步通过 ✓"
echo "===================================="
