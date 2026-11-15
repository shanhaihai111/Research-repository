# Edit only these two paths after copying the project to Windows.
$Project = "C:\\Research\\psychiatry_frontiers_weekly"
$Python = "C:\\Python312\\python.exe"
Set-Location $Project
& $Python .\\src\\run_weekly.py --no-email
exit $LASTEXITCODE
