"""
Prompt 模板管理
集中管理所有 LLM 调用的 prompt，方便调试和迭代
"""

from __future__ import annotations


class Prompts:
    """所有 Prompt 模板的集中管理"""

    # ═══════════════ Phase1: CodeGraph 语义层 ═══════════════

    SUMMARIZE_FUNCTION = """You are a code analyst. Analyze this function and respond in JSON.

Function name: {name}
Language: {language}
Code:
```
{code}
```

Respond in JSON:
{{"summary": "<what this function does in ONE concise sentence, max 30 words, focus on purpose not implementation>", "category": "<one of: business | utility | infrastructure | config | test>", "tags": ["<2-5 lowercase functional keywords describing WHAT this code does, e.g. authentication, database_query, file_upload>"]}}

Category guide:
- business: domain/feature logic (handlers, services, models, validators specific to domain)
- utility: reusable helpers (string formatting, math, data conversion, generic validation)
- infrastructure: framework/system plumbing (logging, DB connection, middleware, serialization)
- config: configuration, constants, environment setup
- test: test code, fixtures, mocks

Tags guide:
- Use 2-5 lowercase keywords/phrases describing the functional domain
- Focus on WHAT the code does (e.g. "user_registration", "order_payment"), not HOW (e.g. "loop", "if_statement")
- Use underscores for multi-word tags"""

    SUMMARIZE_FUNCTION_RETRY = """You are a code analyst. A previous attempt to summarize this function produced a vague result.
Generate a MORE SPECIFIC summary that captures the function's UNIQUE purpose.

Function name: {name}
Language: {language}
Code:
```
{code}
```

Call context (who calls this / what this calls):
{call_context}

Other functions in the same file:
{file_context}

Previous (vague) summary: {previous_summary}

Requirements:
- The new summary MUST mention specific domain concepts, data types, or operations
- Do NOT just repeat the function name as the summary
- Focus on WHAT makes this function different from its siblings

Respond in JSON:
{{"summary": "<specific sentence, max 30 words>", "category": "<one of: business | utility | infrastructure | config | test>", "tags": ["<2-5 lowercase functional keywords>"]}}"""

    SUMMARIZE_CLASS = """You are a code analyst. Summarize what this class/module is responsible for in ONE concise sentence (max 40 words).

Class name: {name}
Language: {language}
Method summaries:
{method_summaries}

Respond with ONLY the summary sentence."""

    SUMMARIZE_FILE = """You are a code analyst. Summarize the responsibility of this source file in ONE sentence (max 50 words).

File: {file_path}
Language: {language}
Contains:
{contents_summary}

Respond with ONLY the summary sentence."""

    SUMMARIZE_MODULE = """You are a code analyst. Summarize the responsibility of this module/directory in ONE sentence (max 50 words).

Module: {module_path}
File summaries:
{file_summaries}

Respond with ONLY the summary sentence."""

    # ═══════════════ Phase2: CodePrune 锚点 + 闭包 ═══════════════

    UNDERSTAND_INSTRUCTION = """You are analyzing which code entities implement a user's feature request in a code repository.

=== User's Request ===
{user_instruction}

=== Repository Module Overview ===
{directory_summaries}

=== Candidate Code Entities ===
(Ranked by semantic similarity to the request. Format: [rank] [TYPE] qualified_name — summary (file))
{entity_list}

=== Other Files in Repository (not shown above) ===
{other_files}

### Your Task

1. **Decompose** the request into independent sub-features.
   Each sub-feature should map to a distinct area of the codebase.

   ⚠ GROUNDING RULE: Every sub-feature MUST directly correspond to an explicit statement
   in the user's request. Do NOT infer or add sub-features the user did not mention —
   even if they seem "obviously needed" (e.g. do NOT add "data models" unless the user
   explicitly mentions them). The dependency solver will automatically pull in required
   supporting code; your job is to capture ONLY what the user explicitly asked for.

2. **For each sub-feature**, identify 1~3 "implementation roots" from the Candidate list above.
   An implementation root is:
   - A function or class whose DEPENDENCY CHAIN covers most of the sub-feature's logic
   - The "starting point" for tracing what code is needed
   - NOT a generic utility (like hashPassword, formatDate) that many features share
   - NOT a whole file/directory — prefer the specific function/class inside it
   If a good root is NOT in the candidate list but you see it in "Other Files", mention it in reasoning.

3. **Identify out-of-scope items** (`out_of_scope`): files, directories, or modules to EXCLUDE from the pruned output.
   Sources:
   a) Items the user explicitly asks to DELETE or REMOVE (删除/去掉/去除/移除) — list them by relative path
   b) Directories or modules clearly OUTSIDE the requested functionality
   Output as relative paths (e.g. ["auth.py", "posts.py", "tags/", "api/userApi.ts"]).
   Prefer file-level paths when the instruction names specific files.

   ⚠ CRITICAL RULE: Only add a WHOLE directory (ending with "/") to out_of_scope if the user explicitly requests deleting the ENTIRE directory. If the instruction says to keep SOME files in a directory while removing others, do NOT add that directory — instead list only the specific files to be removed. Example: if instruction says "db/ 只保留 connection.py, 删除 cache.py", output ["db/cache.py"] NOT ["db/"].

4. **Estimate anchor strategy**:
   - "focused": simple feature, 1~3 entry points total
   - "distributed": moderate features, 4~8 entry points
   - "broad": very complex or cross-cutting features, 8+ entry points

Respond in JSON:
{{
  "sub_features": [
    {{
      "description": "...",
      "root_entities": ["qualified_name_from_candidate_list"],
      "reasoning": "why these roots cover this sub-feature"
    }}
  ],
  "out_of_scope": ["dir_name/", ...],
  "anchor_strategy": "focused" | "distributed" | "broad"
}}"""

    VERIFY_ANCHOR = """You are deciding whether a code entity should be KEPT in a pruned sub-repository.

The user wants to extract specific functionality from a codebase. Your job is to determine if this entity is an implementation root of the code the user wants to **KEEP** (not the code they want to remove).

IMPORTANT: If the user's instruction mentions removing/excluding certain code, any entity that belongs to the REMOVED/EXCLUDED part must be scored below 0.4, even if it is mentioned in the instruction.

=== User Instruction ===
{features_text}
{exclusions_section}

=== Code Entity ===
- Name: {name}
- Type: {node_type}
- Summary: {summary}
- File: {file_path}
{call_context}

Is this entity an implementation root of the code the user wants to KEEP?

Scoring guide:
- 0.9+: Core entry point of the feature to KEEP (handler, controller, service main method)
- 0.7-0.9: Important supporting logic specific to the feature to KEEP
- 0.4-0.7: Shared utility that happens to be used by the feature to KEEP
- <0.4: Belongs to the REMOVED/EXCLUDED part, or is unrelated

Respond in JSON: {{"relevant": true/false, "confidence": 0.0-1.0, "reason": "..."}}"""

    JUDGE_SOFT_DEPENDENCY = """You are deciding whether a code entity should be included in a pruned sub-repository.

User's feature request: "{user_instruction}"

The following entity is a soft dependency (semantically related but not directly called) of already-selected code:
- Name: {name}
- Type: {node_type}  
- Summary: {summary}
- File: {file_path}

Already selected entities:
{selected_summaries}

Should this entity be included to ensure the feature works correctly and completely?
Respond in JSON: {{"include": true/false, "reason": "..."}}"""

    JUDGE_STRUCTURAL_GAP = """You are pruning a code repository to extract a specific feature into an independent sub-repository.

User's feature request: "{user_instruction}"

The following code entity is outside the semantic scope of the requested feature, but is directly referenced by already-selected code:

  Entity: {name} ({node_type})
  File: {file_path}
  Summary: {summary}

  Called by these selected entities:
{callers_context}

  This entity is also used by {other_count} other entities in the repository:
{other_callers}

Choose the most appropriate action:
- "include": This is a core dependency of the target feature — must be kept in full
- "stub": This does not belong to the target feature, but is called — generate a stub to ensure compilation
- "exclude": This is an optional/decorative call (logging, metrics, notifications) — safe to remove the call

Respond in JSON: {{"decision": "include|stub|exclude", "reason": "..."}}"""

    JUDGE_STRUCTURAL_GAP_BATCH = """You are pruning a code repository to extract a specific feature into an independent sub-repository.

User's feature request: "{user_instruction}"

The following {count} entities are outside the semantic scope but referenced by already-selected code:
{entities_text}

Already selected entities:
{selected_summaries}

For each numbered entity, choose: "include" (core dependency), "stub" (generate placeholder), or "exclude" (safe to remove call).
Respond in JSON: {{"decisions": ["include|stub|exclude", ...]}}
Array length MUST be {count}."""

    VERIFY_CORE_INCLUSIONS = """You are pruning a code repository to extract a specific feature.

User's feature request: "{user_instruction}"

The following {count} entities were classified as high-relevance by embedding similarity and auto-included in the closure.
Review each and decide whether it is TRULY needed for the requested feature:

{entities_text}

Already confirmed entities (anchors + structural deps):
{selected_summaries}

For each numbered entity, respond "keep" (genuinely needed) or "reject" (false positive — similar topic but not functionally required).
Respond in JSON: {{"decisions": ["keep|reject", ...]}}
Array length MUST be {count}."""

    # ═══════════════ Phase2: 分层语义评估 (Hierarchical Scope) ═══════════════

    CLASSIFY_DIRECTORIES = """You are a code architecture analyst. Given the user's requirement and a list of source directories, classify each directory.

## User Requirement
{features_text}

## Explicitly Out of Scope
{out_of_scope_text}

## Directories
{dir_entries}

## Classification Rules
- INCLUDE — Contains code that directly implements OR is structurally required by the requirement
- EXCLUDE — Clearly unrelated to the requirement (e.g. a "notifications" module when the requirement is about "order processing")
- When in doubt, classify as INCLUDE (downstream will refine at file level)

Respond in JSON:
{{"directories": {{{dir_keys}}}}}
Each value must be "INCLUDE" or "EXCLUDE"."""

    CLASSIFY_FILES = """You are a code analyst. Given the user's requirement, classify each source file by its role.

## User Requirement
{features_text}

## Files
{file_entries}

## Classification Rules
- CORE — Directly implements the requirement (contains business logic for the described features)
- PERIPHERAL — Does NOT implement the requirement itself, but is structurally needed by CORE code (e.g. database models used by CORE services, shared utilities called by CORE, base classes inherited by CORE)
- OUTSIDE — Not related to the requirement and not needed by CORE code

Important: Err on the side of PERIPHERAL over OUTSIDE. Downstream structural analysis will further filter PERIPHERAL code. Incorrectly marking PERIPHERAL as OUTSIDE causes recall loss.

Respond in JSON: {{{file_keys}}}
Each value must be "CORE", "PERIPHERAL", or "OUTSIDE"."""

    # ═══════════════ Phase3: CodeHeal 自愈 ═══════════════

    FIX_SYNTAX_ERROR_SR = """You are fixing compilation errors in a pruned code repository.
The code was extracted from a larger repository. Errors are caused by pruned dependencies.

CRITICAL RULES:
1. Fix ONLY the reported errors. Make MINIMAL changes.
2. Prefer commenting out or removing problematic code over inventing new logic.
3. Use ONLY patterns from the original repository context provided.
4. Each SEARCH block must contain EXACT lines from the current file.
   Include 2-3 lines of unchanged context before and after the target lines.
5. Never invent new business logic.

C/C++ SPECIFIC RULES (if the file is .c or .h):
6. NEVER wrap or disable existing #include directives with #if 0, comments, or any preprocessor guard. If a #include is present, trust that the header exists.
7. NEVER generate static function stubs for functions declared in included headers. The linker will resolve them.
8. If you see an #include for a header, do NOT add a comment saying the header "was pruned" or "is not present".

=== Errors ({error_count} total) ===
{all_errors}

=== Current Files ===
{files_with_errors}

=== Original Repository Context ===
{original_context}
{reflected_history}
{dispatcher_context}
Output SEARCH/REPLACE blocks for ALL fixes. Each block must follow this exact format:

path/to/file.ext
<<<<<<< SEARCH
exact existing lines from the file
=======
replacement lines
>>>>>>> REPLACE

Rules for SEARCH/REPLACE blocks:
- The SEARCH section must match EXACTLY what is in the current file (including whitespace).
- One block per fix. Multiple blocks for the same file are allowed.
- Fix foundational errors FIRST (missing includes/imports before usage errors).
- If you cannot fix an error, skip it — do NOT output an empty SEARCH block."""

    ARCHITECT_ANALYZE_ERRORS = """You are an expert code repair architect. Analyze these compilation errors from a pruned code repository and create a prioritized repair plan.

The code was extracted from a larger repository. Errors are likely caused by missing dependencies that were pruned away.

=== All Errors ({error_count} total) ===
{errors_summary}

=== Files with Errors ===
{files_content}

=== Repair Strategy Guide ===
- COMMENT: Comment out the problematic line(s) — best when code references removed dependencies with no local substitute
- PATCH: Replace code with a minimal fix — best when a small change (e.g. add an import, remove a parameter) resolves the error
- STUB: A missing type/class needs a stub — best when a type is needed for compilation but its implementation was pruned
- SKIP: Cannot be fixed automatically — requires understanding business logic

RULES:
1. Fix foundational errors FIRST (missing imports before usage errors, base classes before subclasses)
2. Group related errors — fixing one may resolve others
3. Be specific in hints — reference exact line numbers and code

Respond in JSON:
{{"plan": [{{"file": "relative/path", "error_snippet": "brief error text", "priority": 1, "strategy": "COMMENT|PATCH|STUB|SKIP", "hint": "specific guidance for implementing the fix"}}]}}"""

    FIX_SYNTAX_ERROR = """You are fixing a compilation/syntax error in a pruned code repository.
The code was extracted from a larger repository. The error is likely due to missing dependencies that were pruned away.

CRITICAL RULES:
1. Fix ONLY the reported error, make minimal changes
2. Prefer removing/commenting problematic code over generating new logic
3. If you must add code, use ONLY patterns from the original repository context provided
4. Never invent new business logic

C/C++ SPECIFIC RULES (if the file is .c or .h):
5. NEVER wrap or disable existing #include directives with #if 0, comments, or any preprocessor guard. If a #include is present, trust that the header exists.
6. NEVER generate static function stubs (e.g., static void foo() {{}}) for functions that should come from included headers. If the function is declared in a header that is #include'd, the linker will resolve it.
7. If you see an #include for a header, do NOT add a comment saying the header "was pruned" or "is not present". The build system manages include paths.

Error:
{error_message}

Error location (█ marks error lines):
```
{error_context}
```

Full file ({file_path}):
```
{file_content}
```

Original repository context (relevant files):
{original_context}

Respond in JSON: {{"file_path": "...", "original_code": "exact lines to replace", "fixed_code": "replacement", "explanation": "..."}}"""

    FIX_UNDEFINED_NAMES = """You are fixing undefined name errors in a pruned code repository.
These names are unresolved after automated deterministic import fixing. They require your judgment.

CRITICAL RULES:
1. Fix ONLY the undefined names listed below. Make minimal changes.
2. PREFER commenting out lines that reference pruned-away dependencies over inventing replacements.
3. If a name clearly comes from a known module (see CodeGraph context), add the correct import.
4. If a name is used as a type annotation only, replace with `Any` and add `from typing import Any`.
5. Never invent business logic. Never guess what a function does.

=== Undefined Names in {file_path} ===
{undefined_names_detail}

=== File Content ===
```
{file_content}
```

=== CodeGraph Context (definitions found in original repo) ===
{graph_context}

=== Available Modules in Sub-Repo ===
{available_modules}

=== Original Repository Context ===
{original_context}

Respond in JSON with an array of fixes:
{{"fixes": [{{"original_code": "exact line(s) to replace", "fixed_code": "replacement code", "explanation": "why this fix"}}]}}

Each fix should address one or more undefined names. Group related fixes when they share the same line."""

    CHECK_COMPLETENESS = """You are checking if a pruned sub-repository completely implements a requested feature.

User's feature request: "{user_instruction}"

Sub-repository file summaries:
{sub_repo_summaries}

Original repository's relevant module summaries:
{original_summaries}

Are there any critical components missing from the sub-repository that are needed for the feature to work?
Respond in JSON: {{"complete": true/false, "missing_components": ["..."], "explanation": "..."}}"""

    # ═══════════════ Finalize: 子仓库产物生成 ═══════════════

    GENERATE_SUB_REPO_README = """You are documenting a focused code module that was automatically extracted from a larger repository.

=== Extraction Context ===
Source repository: {repo_name}
User's extraction request: "{user_instruction}"

=== Sub-Repository Files ===

{file_details}

=== Internal Dependency Graph ===
{dependency_graph}

=== External Dependencies ===
{external_deps_text}

=== Excluded Components ===
{pruned_info}

=== Instructions ===
Write a README.md with these sections:

1. **Title** — A short, descriptive module name reflecting the extracted functionality
2. **Overview** — 2-3 sentences: what this code does and that it was extracted from {repo_name}
3. **File Structure** — Table with columns: file | lines | description (one-line)
4. **Core API** — Key public functions/classes grouped by file, with their signatures and one-line descriptions. ONLY list functions/classes shown in "Sub-Repository Files" above. Do NOT invent any.
5. **Architecture** — Briefly describe how the files depend on each other, based on the dependency graph above
6. **Dependencies** — List external packages needed. If none, say "仅依赖 Python 标准库" (or equivalent)
7. **Quick Start** — A realistic usage example using ONLY the actual function signatures listed above. Do NOT invent parameters or functions.
8. **Notes** — What was excluded from the original repository (based on "Excluded Components" above), and any potential limitations

Rules:
- Write the entire README in {language}
- Be concise and professional — no boilerplate, no badges, no license section
- Code examples must reference only real function names from the listings above
- If a function's signature is not available, use just its name
- Use proper Markdown formatting"""
