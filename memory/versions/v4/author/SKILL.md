# 出题者记忆：说明

出题者记忆的类别由程序确定，不经过判断者：程序读取本里程碑行为清单中每条描述的类型（主路径、边界、状态转换、异常、组合、协议），
按这些类型召回出题经验，注入出题者提示词。若该里程碑没有行为清单，程序先生成清单；生成失败则报错停止。

若将来需要判断者，流程同其他记忆库：
1. 先读里程碑目标、验收标准与行为清单；
2. 逐个类别核对清单条目的类型；
3. 只输出 {"domain_tags": [...], "selected": [...], "skill_version": "..."}；
4. 不得编造类别，不得输出类别目录以外的内容。

## 如何测试依赖外部世界的行为

When the behaviour under test talks to the outside world (network, other processes, the file
system outside the test's own directory, a user's home or configuration), keep the library the
program calls real and replace only the world it talks to with a real local one. A hand-written
stand-in for the library itself encodes your guess about its interface, and a correct
implementation that calls the real interface differently then fails.

Tools of `adamas_testkit` (importable at module top in gate and verification suites):
- a download or an HTTP API: `local_http_server(files)`, plain or streamed bodies, gives a base URL;
- cloning or checking out a repository: `local_git_repo(files, commits, branches, tags)` with real git, gives a path or `file://` URL;
- a protocol client (mail, chat, a custom wire format): `local_tcp_server(script)` answers a scripted dialogue;
- configuration or state under the user's home: `temp_home()`;
- an external command that must not really run: `fake_command(name, script)`, prepended to the original PATH and run by an absolute-path interpreter.

When none of these can stand in, build the double from the real object with
`unittest.mock.create_autospec(...)` or `Mock(spec=...)`, never by hand. Assert only what the
documents say is observable (return values, file contents, state, raised exceptions), not how the
program called its dependency, unless the documents prescribe the call.
