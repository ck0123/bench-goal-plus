---
name: rea-analysis
description: Use REA for binary-level verification while crafting the 仓库审计 — compile suspicious code from public/ and confirm the exact crash path with objdump disassembly / DWARF / mitigation checks. Available commands: inspect-binary-layout, inspect-with-objdump, analyze-javascript-application.
metadata:
  version: "1"
---

# REA 二进制级验证（仓库审计）

## 何时使用

L1 任务要求写出能在漏洞版本中触发崩溃、在修复版本中不触发的 `poc`。当 `public/`
源码阅读无法确定**精确触发条件**时，用 REA 在二进制级确认。仅在以下情况使用：

- 疑似内存破坏点找到了，但不确定确切的边界长度、结构体偏移或触发序列
  （如 off-by-one、整数溢出后的分配大小、UAF 的重用顺序）
- 需要确认编译后的真实控制流（宏展开、内联后的检查是否还存在）
- 需要确认目标代码的缓解（canary / RELRO / NX / PIE）以选择触发策略

**不要用 REA 做纯源码阅读或架构浏览**——那是普通文件工具的工作，也是默认路径。

## 环境说明

沙箱内 REA 通过 `rea` 命令可用（node 运行时，只读挂载在 /opt/rea）。gcc/cc 在
PATH 中可用（宿主 /usr 只读）。scratch 文件一律写 `/tmp`（沙箱私有 tmpfs）。
workspace 除 `poc` 外只读；编译产物和中间文件只能放 `/tmp`。

## 可用命令

### 编译可疑代码并反汇编（最可靠入口）

```bash
# 从 public/ 提取可疑翻译单元，在 /tmp 独立编译
gcc -o /tmp/check -g -I public/<include-paths> public/src/suspect.c

# 反汇编确认崩溃路径（objdump 在 /usr/bin，无需 Ghidra）
rea inspect-with-objdump /tmp/check disassemble --json
rea inspect-with-objdump /tmp/check symbols --json
rea inspect-with-objdump /tmp/check dwarf --json        # 源码行映射
```

objdump 文本在 `normalized_result.output`（上限 16 MiB）。定位关键函数：
先 `symbols` 找地址，再按地址段读 `disassemble`。

### 检查安全缓解

```bash
rea inspect-binary-layout /tmp/check --json
```

关键输出字段（`normalized_result.mitigations`）：
- `stack_canary_indicator` — 栈保护（`__stack_chk_fail`）
- `relro` — "Full" / "Partial" / null
- `nx_indicator`、`position_independent`（PIE）

注意：`inspect-binary-layout` 需要 pwntools Python 环境，沙箱内可能不可用；
不可用时以 `inspect-with-objdump` 为准。

### JS 应用分析（仅 jiuwenswarm 类任务）

```bash
rea analyze-javascript-application <目录> --json
```

### 其他

```bash
rea --help    # 完整命令列表
```

## 典型工作流

1. **定位可疑点**：在 `public/` 源码中找到疑似缺陷（如某 `memcpy` 前的长度检查）
2. **独立编译**：把相关翻译单元（含头文件路径）编译到 `/tmp/check`
3. **二进制级确认**：`rea inspect-with-objdump /tmp/check disassemble --json`
   —— 确认编译后检查指令是否真的存在、边界常量是多少
4. **推导触发输入**：由真实的边界/偏移反推 `poc` 需要构造的输入长度与内容
5. **写 poc**：把结论落进仓库根部的 `poc`；本地无法完整复现 judge 环境，
   以官方差分结果为最终判据

## 输出解读

每个 REA 命令返回 **Evidence record**：

```json
{
  "evidence_id": "...",
  "normalized_result": { ... },
  "confidence": "high|medium|low",
  "limitations": ["..."],
  "unknowns": ["..."]
}
```

- 二进制证据只反映**你本地编译的那个二进制**，与 judge 镜像内的构建
  (编译选项、加固、依赖版本)可能不同——用它确定代码语义，不要用它推断
  judge 的崩溃表现
- `limitations` / `unknowns` 必须在结论中考虑

## 限制

- 本地编译 ≠ judge 环境：官方判定仍以 Docker 内 vuln/fix 双镜像差分为准
- `inspect-binary-layout` 仅 Linux x64 ELF 且依赖 pwntools（沙箱内可能不可用）
- 深度反编译（`decompile`）需要 Ghidra，沙箱内不可用
- REA 无网络访问；所有分析只针对本地文件
