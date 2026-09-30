@echo off
REM Ежедневный запуск skill geeklinker-fallback через Планировщик заданий Windows.
REM Создано задачей "GeekLinker-AI-Fallback" (см. MANUAL.md - как включить/выключить).

cd /d "C:\OSPanel\domains\geeklinker\geeklinker_bot"

"C:\Users\Admin\AppData\Roaming\npm\claude.cmd" -p "Выполни skill geeklinker-fallback до конца. Работай автономно, ни о чём не спрашивай. В конце дай короткий отчёт: сколько источников проверено, сколько статей добавлено, были ли ошибки." --permission-mode bypassPermissions --allowedTools "Bash,Read,Write,Edit,Glob,Grep" >> "C:\OSPanel\domains\geeklinker\geeklinker_bot\.claude\skills\geeklinker-fallback\run_log.txt" 2>&1

echo ---%date% %time%--- >> "C:\OSPanel\domains\geeklinker\geeklinker_bot\.claude\skills\geeklinker-fallback\run_log.txt"
