# Vitals Deck 插件 SDK

Deck 插件可以提供按键动作、按键状态和按键显示变量。插件以 Vitals 主机服务的权限运行，请只安装并启用你信任的代码。浏览器可以读取插件说明并使用已启用的插件。安装与启用由本机管理员通过文件完成。

## 安装并试用示例

1. 在主机的 `config.json` 同级目录创建 `plugins` 文件夹。直接从源码运行时，仓库已包含 [`_example`](_example/manifest.json)。使用桌面版时，将 `_example` 文件夹复制到主机配置目录下的 `plugins` 文件夹。
2. 在 `config.json` 的 `deck` 对象中加入 `enabled_plugins`。已经存在的 Deck 设置和插件 ID 可以保留。

   ```json
   {
     "deck": {
       "enabled_plugins": ["_example"]
     }
   }
   ```

3. 重启 Vitals 主机服务。编辑 Deck 按键时选择示例的「增加计数」动作，可同时附加「达到目标」状态或「当前计数」显示。示例计数保存在进程内，服务重启后归零。

修改插件文件或 `enabled_plugins` 后，重启主机服务使改动生效。把插件 ID 从列表中移除后，重启即可停用。

## 插件文件

每个插件使用固定文件名 `plugins/<id>/manifest.json` 和 `plugins/<id>/plugin.py`。创建自己的插件时，复制示例并修改目录名及清单的 `id`。ID 长度为 1–48 个字符，以小写英文字母或数字开头，可包含小写英文字母、数字、连字符和下划线。`_example` 是示例插件专用 ID。

[`manifest.json`](_example/manifest.json) 使用 UTF-8，包含以下字段。

| 字段 | 含义 |
| --- | --- |
| `id` | 插件 ID，必须与目录名一致。 |
| `name` | 本地化名称对象，例如 `{"en": "Counter", "zh-CN": "计数器"}`。 |
| `version` | 版本字符串。 |
| `actions` | 动作列表，每项包含 `id`、`name` 与可选 `parameters`。 |
| `feedbacks` | 状态列表，每项包含 `id`、`name` 与可选 `parameters`。 |
| `variables` | 显示变量列表，每项包含 `id`、`name` 和 `type`。 |

各列表可以省略。动作、状态、变量及参数的 ID 也遵循上述字符规则，且在所属列表内唯一。参数包含 `id`、`name`、`type`，可以附加 `required`（默认 `false`）、`default` 和 `choices`。`choices` 是使用同一类型的候选值列表，若配置了默认值，它必须在候选列表中。未提供的参数使用默认值，没有默认值的必填参数必须由按键提供。没有默认值的选填参数会从传入字典省略。

| `type` | 接受的 JSON 值 |
| --- | --- |
| `string` | 字符串 |
| `integer` | 整数 |
| `number` | 有限数字，支持整数和小数 |
| `boolean` | `true` 或 `false` |

## Python 接口

在 [`plugin.py`](_example/plugin.py) 中定义同步函数。主中心启动时扫描清单并加载已启用的插件。Deck 在调用函数前验证参数，同一插件的调用依次执行。

- `execute_action(action_id, params)` 必须提供。`action_id` 是清单中声明的动作 ID，`params` 是校验后的参数字典。函数可以返回可序列化的 JSON 值，也可以返回 `None`。
- `get_state(feedback_id, params)` 用于清单声明的状态。Deck 会读取它来更新按键。返回 `True` 或 `False` 可表示状态是否激活。
- `get_variables()` 返回由变量 ID 映射到当前值的字典。字典中的每项都必须由清单声明，并且值的类型与清单一致。暂时无值的变量可以省略。

状态及变量会定期读取，请让这些函数尽快返回并避免产生副作用。函数返回的值可能显示在远程 Deck 上，请将其视为用户可见数据。Deck 对外返回固定错误消息。

## Deck 配置

`GET /api/deck/catalog` 的 `plugins` 包含发现的插件清单和 `enabled` 状态。保存 Deck 按键时，插件动作、状态及显示变量可分别使用以下结构。

| 用途 | 内容 |
| --- | --- |
| 动作 | `{"id":"plugin","params":{"plugin_id":"_example","action_id":"increment","args":{"amount":2}}}` |
| 状态 | `{"plugin_id":"_example","feedback_id":"at-target","args":{"target":5}}` |
| 显示变量 | `{"plugin_id":"_example","variable_id":"count"}` |
