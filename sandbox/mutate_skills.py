"""Mutation checks for skills, custom commands and plugins (2026-11-10): where they are looked for, how a ``SKILL.md`` is read, how a
message reaches the model with them (the hook, for every engine; the ``skill`` tool and the list in the instructions, for the own
loop), the permission to read a skill's folder, and the AppCore method that lists them. Each mutant undoes one part; the tests must
catch it.

    python sandbox/mutate_skills.py            run every mutant, in a temporary copy of the repository
    python sandbox/mutate_skills.py --check    only check that every pattern is found exactly once (read only)
    python sandbox/mutate_skills.py 3 7        run just those mutants

The machinery is that of ``mutate_harness_g678.py`` (this working tree is never written to); only the table and the tests differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate_harness_g678 as base  # noqa: E402

CORE = Path("desktop") / "core"
HARNESS = CORE / "harness"
SK, TOOL, PERM, PROMPT, TOOLS = (HARNESS / name for name in ("skills.py", "skill_tool.py", "permissions.py", "prompt.py", "tools.py"))
CONTROLLER, APP = CORE / "chatgpt" / "controller.py", CORE / "app_core.py"

NEW = ["tests/test_harness_skills.py", "tests/test_app_commands.py"]
OLDER = ["tests/test_harness_tools.py", "tests/test_harness_permissions.py", "tests/test_harness_prompt.py", "tests/test_harness_toolset.py", "tests/test_chatgpt_controller.py"]

SOURCES = '    for part in (".factory", ".agents", ".agent", ".claude"):\n        out.append(Source("personal", home / part / "skills"))\n'
PROJECTS = '    for folder in _repository_folders(project, home):\n        out += [Source("project", folder / part / "skills") for part in (".factory", ".agents", ".agent", ".claude")]\n'

M = [
    # ---- the front matter
    ("front matter: a quoted value keeps its quotes", SK, "        return value[1:-1]\n", "        return value\n"),
    ("front matter: a byte order mark is part of the file", SK, '    text = text.lstrip("\\ufeff")\n', "    pass\n"),
    ("front matter: an unclosed block is all front matter", SK, "    if end is None:\n        return {}, text\n", "    if end is None:\n        end = len(lines)\n"),
    ("front matter: a block ends only with three dashes", SK, 'lines[i].strip() in ("---", "...")', 'lines[i].strip() == "---"'),
    ("front matter: true and false are words", SK, '    if isinstance(value, str) and value.strip().lower() in ("true", "false"):\n', "    if False:\n"),
    ("front matter: a name keeps its capitals and hyphens at the ends", SK, '    return _NAME.sub("-", text.lower()).strip("-")\n', "    return _NAME.sub(\"-\", text.lower())\n"),
    ("front matter: a name keeps its capitals", SK, '    return _NAME.sub("-", text.lower()).strip("-")\n', '    return _NAME.sub("-", text).strip("-")\n'),
    ("front matter: folded text keeps its lines", SK, '(" " if rest[0] == ">" else "\\n").join(', '("\\n" if rest[0] == ">" else "\\n").join('),
    ("front matter: a literal block is folded", SK, '(" " if rest[0] == ">" else "\\n").join(', '(" " if rest[0] == ">" else " ").join('),
    ("front matter: [a, b] is one word", SK, '        elif rest.startswith("["):\n', "        elif False:\n"),
    ("front matter: a plain value does not go on over lines", SK, '            continuation = [part.strip() for part in block if part.strip() and not part.strip().startswith("- ")]\n', "            continuation = []\n"),
    ("front matter: a list under a key is not read", SK, '            items = [_unquote(part.strip()[2:]) for part in block if part.strip().startswith("- ")]\n', "            items = []\n"),
    # ---- where they are looked for
    ("sources: the search does not stop at the repository's root", SK, '        if (folder / ".git").exists() or folder in (home, real) or folder.parent == folder:\n', "        if folder in (home, real) or folder.parent == folder:\n"),
    ("sources: the home folder's own .factory counts as the project's", SK, "        if folder not in (home, real):", "        if True:"),
    ("sources: the farthest parent comes first", SK, "            found.append(folder)\n", "            found.insert(0, folder)\n"),
    ("sources: the project's settings turn nothing off", SK, 'for path in (home / ".factory" / "settings.json", project / ".factory" / "settings.json"):', 'for path in (home / ".factory" / "settings.json",):'),
    ("sources: the user's settings turn nothing off", SK, 'for path in (home / ".factory" / "settings.json", project / ".factory" / "settings.json"):', 'for path in (project / ".factory" / "settings.json",):'),
    ("sources: the project's .claude folder is not looked at", SK, PROJECTS, PROJECTS.replace(', ".claude")', ")")),
    ("sources: the user's .agent folder is not looked at", SK, SOURCES, SOURCES.replace('".agents", ".agent", ', '".agents", ')),
    ("sources: the user's skills come before the project's", SK, PROJECTS + SOURCES, SOURCES + PROJECTS),
    ("sources: plugins come before the user's skills", SK, '    out += [Source("plugin", root / "skills", root) for root in _plugins(home)]\n    return out\n', '    return [Source("plugin", root / "skills", root) for root in _plugins(home)] + out\n'),
    ("sources: plugins bring no commands", SK, '    out += [Source("plugin", root / "commands", root) for root in _plugins(home)]\n', ""),
    ("sources: the user's commands are not looked at", SK, '    out.append(Source("personal", home / ".factory" / "commands"))\n', ""),
    ("plugins: only the Factory layout is a plugin", SK, 'if ".factory-plugin" in names or ".claude-plugin" in names:', 'if ".factory-plugin" in names:'),
    ("plugins: only the Claude layout is a plugin", SK, 'if ".factory-plugin" in names or ".claude-plugin" in names:', 'if ".claude-plugin" in names:'),
    ("plugins: the search has no depth limit", SK, "        if depth >= PLUGIN_DEPTH:\n", "        if False:\n"),
    ("plugins: one turned off is used all the same", SK, '        if any(key.split("@")[0] == name and value is False for key, value in switched.items() if isinstance(key, str)):\n            continue\n', ""),
    ("plugins: a plugin turned off by the name of its folder is used all the same", SK, "        name = name or root.name\n", "        pass\n"),
    ("plugins: the Claude manifest is not read", SK, 'for meta in (root / ".factory-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json"):', 'for meta in (root / ".factory-plugin" / "plugin.json",):'),
    ("plugins: a plugin's own folder is not named in its instructions", SK, "    if plugin is None:\n        return text\n", "    if False:\n        return text\n"),
    ("plugins: only one root variable is known", SK, 'for variable in ("DROID_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):', 'for variable in ("DROID_PLUGIN_ROOT",):'),
    ("plugins: the variable has to have braces", SK, '.replace("${" + variable + "}", str(plugin)).replace("$" + variable, str(plugin))', '.replace("${" + variable + "}", str(plugin))'),
    # ---- a skill
    ("skill: the instructions keep their front matter", SK, "split_front_matter(text)[1].strip()[:BODY_LIMIT], self.plugin)", "text.strip()[:BODY_LIMIT], self.plugin)"),
    ("skill: the instructions have no length limit", SK, "split_front_matter(text)[1].strip()[:BODY_LIMIT], self.plugin)", "split_front_matter(text)[1].strip(), self.plugin)"),
    ("skill: a file that is not there is not empty", SK, '        text = _read(self.path)\n        if text is None:\n            return ""\n        return _expand_root', "        text = _read(self.path)\n        return _expand_root"),
    ("skill: SKILL.md is listed among the files beside it", SK, "                if relative != SKILL_FILE:\n", "                if True:\n"),
    ("skill: the files beside it are not limited", SK, "                if len(found) >= FILES_LISTED:\n", "                if False:\n"),
    ("skill: the files of a folder named node_modules are listed", SK, "            folders[:] = sorted(name for name in folders if name not in _SKIP)\n            for name in sorted(names):\n                relative", "            folders[:] = sorted(folders)\n            for name in sorted(names):\n                relative"),
    ("skill: the search goes on below a skill", SK, "            yield current\n            continue\n", "            yield current\n"),
    ("skill: the search has no depth limit", SK, "        if depth < DEPTH:\n", "        if True:\n"),
    ("skill: the search enters node_modules", SK, "if entry.is_dir(follow_symlinks=False) and entry.name not in _SKIP)", "if entry.is_dir(follow_symlinks=False))"),
    ("skill: a folder called SKILL.md is a skill", SK, "if any(entry.name == marker and entry.is_file() for entry in entries):", "if any(entry.name == marker for entry in entries):"),
    ("skill: one turned off is used", SK, '    if not _flag(data.get("enabled"), True):\n        return None\n', ""),
    ("skill: one without a description is used", SK, "    if not description:\n        return None\n", ""),
    ("skill: the name in the file is not the one used", SK, 'name = slug(str(data.get("name") or path.parent.name))', "name = slug(path.parent.name)"),
    ("skill: its folder's name is not the one used without a name", SK, 'name = slug(str(data.get("name") or path.parent.name))', 'name = slug(str(data.get("name") or ""))'),
    ("skill: the description keeps its line breaks", SK, 'description = " ".join(str(data.get("description") or "").split())', 'description = str(data.get("description") or "")'),
    ("skill: anybody may name it", SK, 'user_invocable=_flag(data.get("user-invocable"), True),', "user_invocable=True,"),
    ("skill: the model may always load it", SK, 'model_invocable=not _flag(data.get("disable-model-invocation"), False),', "model_invocable=True,"),
    ("skill: it does not know the plugin it comes from", SK, "        plugin=source.plugin,\n    )\n", "        plugin=None,\n    )\n"),
    ("discover: the last of two of one name wins", SK, "if skill is not None and skill.name not in found and skill.name not in off:", "if skill is not None and skill.name not in off:"),
    ("discover: one turned off by name is listed", SK, "if skill is not None and skill.name not in found and skill.name not in off:", "if skill is not None and skill.name not in found:"),
    ("read roots: the plugin's folder is not readable", SK, "for root in (skill.folder, skill.plugin):", "for root in (skill.folder,):"),
    ("read roots: a folder is listed once for each skill in it", SK, "            if root is not None and root not in roots:\n", "            if root is not None:\n"),
    # ---- what the model is told
    ("prompt: a skill the model may not load is listed", SK, "    usable = [skill for skill in skills if skill.model_invocable]\n", "    usable = list(skills)\n"),
    ("prompt: no skills is a heading with nothing under it", SK, '    if not usable:\n        return ""\n    lines = [', "    lines = ["),
    ("prompt: every skill is listed", SK, "    for skill in usable[:LISTED]:\n", "    for skill in usable:\n"),
    ("prompt: a long description is not cut", SK, "if len(skill.description) <= DESCRIPTION_LIMIT else", "if True else"),
    ("prompt: the rest is not counted", SK, "    if len(usable) > LISTED:\n", "    if False:\n"),
    ("prompt: the instructions do not list the skills", PROMPT, "    if skills:\n        parts.append(skills_prompt(skills))\n", ""),
    ("tool: it does not tell what is beside the instructions", SK, "    if files:\n        head += ", "    if False:\n        head += "),
    # ---- naming a skill in a message
    ("naming: a skill that may not be named is named", SK, "by_name = {skill.name: skill for skill in skills if skill.user_invocable}", "by_name = {skill.name: skill for skill in skills}"),
    ("naming: a skill named twice is carried twice", SK, "        if skill is not None and skill not in out:\n", "        if skill is not None:\n"),
    ("naming: a path is a name", SK, '_TOKEN = re.compile(r"(?:(?<=\\s)|^)/', '_TOKEN = re.compile(r"/'),
    ("naming: a file name after the skill's is the skill", SK, r'|[.](?:\s|$))")', r'|[.])")'),
    # ---- commands
    ("command: the arguments are not put in", SK, 'body.replace("$ARGUMENTS", arguments.strip())', 'body.replace("$ARGUMENTS", "")'),
    ("command: the arguments are put in with their spaces", SK, 'body.replace("$ARGUMENTS", arguments.strip())', 'body.replace("$ARGUMENTS", arguments)'),
    ("command: its first line is not its description", SK, '(line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")', '(line.strip().lstrip("#").strip() for line in body.splitlines() if False), "")'),
    ("command: the description in the file is not the one used", SK, 'str(data.get("description") or first)', "first"),
    ("command: its argument hint is not kept", SK, 'str(data.get("argument-hint") or "")', '""'),
    ("command: any file is a command", SK, '                if name.lower().endswith(".md"):\n', "                if True:\n"),
    ("command: the last of two of one name wins", SK, "if command is not None and command.name not in found:", "if command is not None:"),
    ("command: folders below the first are not looked at", SK, "if depth < DEPTH else []", "if False else []"),
    ("command: a message with spaces before it is not a command", SK, "    match = _CALL.match(text.strip())\n", "    match = _CALL.match(text)\n"),
    ("listing: a skill of a command's name is listed too", SK, "if skill.user_invocable and skill.name not in names:", "if skill.user_invocable:"),
    ("listing: a skill that may not be named is listed", SK, "if skill.user_invocable and skill.name not in names:", "if skill.name not in names:"),
    ("hook: a command is not run", SK, "        if called is not None:\n", "        if False:\n"),
    ("hook: a command is not told to the user", SK, 'notice=f"已载入命令 /{command.name}"', 'notice=""'),
    ("hook: a skill is not told to the user", SK, 'notice="已载入技能 " + "、".join(skill.name for skill in used)', 'notice=""'),
    ("hook: a failure goes up to the message", SK, '    except Exception:\n        log.exception("reading skills for a message failed")\n        return None\n', '    except Exception:\n        raise\n'),
    # ---- the skill tool and the permission to read
    ("tool: the model may load a skill it may not", TOOL, "if skill.model_invocable]", "]"),
    ("tool: a name must be typed as in the file", TOOL, 'wanted = skills.slug(str(args.get("skill") or ""))', 'wanted = str(args.get("skill") or "")'),
    ("tool: an unknown skill is not an error", TOOL, "Available skills: {names}.\", is_error=True)", "Available skills: {names}.\", is_error=False)"),
    ("tool: an unknown skill does not name the ones there are", TOOL, 'names = ", ".join(item.name for item in available) or "none"', 'names = "none"'),
    ("tool: the page is not told which skill", TOOL, '    return {"skill": name} if isinstance(name, str) else {}\n', "    return {}\n"),
    ("tool: it is a change as far as the permissions go", TOOL, '    kind="read",\n)', '    kind="edit",\n)'),
    ("tool: it is not among the tools", TOOLS, "        skill_tool.register(registry)\n", ""),
    ("permissions: the skill tool asks", PERM, '_NO_EFFECT = frozenset({"todo_write", "skill"})', '_NO_EFFECT = frozenset({"todo_write"})'),
    ("permissions: a skill's folder asks", PERM, '        if all(flag or _readable(path, ctx) for path, flag in touched):\n', "        if False:\n"),
    ("permissions: only the folder itself is readable", PERM, "path == root or root in path.parents for root in ctx.readable", "path == root for root in ctx.readable"),
    ("permissions: only what is under the folder is readable", PERM, "path == root or root in path.parents for root in ctx.readable", "root in path.parents for root in ctx.readable"),
    # ---- in the conversation and AppCore
    ("controller: the folders of the skills are not readable", CONTROLLER, "            self._context.readable = skills.read_roots(found)\n", "            pass\n"),
    ("controller: the model is not told which skills there are", CONTROLLER, "plan=plan, skills=found\n", "plan=plan\n"),
    ("controller: the skills are read once", CONTROLLER, "            found = await asyncio.to_thread(skills.discover, self.project)\n", "            if not hasattr(self, \"_found\"):\n                self._found = await asyncio.to_thread(skills.discover, self.project)\n            found = self._found\n"),
    ("app: the hook is not on the prompt", APP, "        manager.hooks.on_prompt(skills.prompt_hook)\n", ""),
    ("app: the list is of the home folder", APP, '        if params.get("id"):\n            project = self._conversation(params["id"]).project\n', "        if False:\n            pass\n"),
    ("app: the list ignores the project asked for", APP, '        elif params.get("projectId"):\n            project = str(params["projectId"])\n', ""),
    ("app: the list has no project to fall back on", APP, "        if not project:\n            project = self.settings.last_project or str(Path.home())\n", ""),
]

if __name__ == "__main__":
    base.M[:] = M
    base.NEW[:] = NEW
    base.OLDER[:] = OLDER
    sys.exit(base.main())
