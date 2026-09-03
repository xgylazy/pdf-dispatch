$pwd = Read-Host "SSH password" -AsSecureString
$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pwd)
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(BSTR)
$host = "root@192.192.98.86"
$commands = @(
    "ls -la /opt/pdf-scheduler/",
    "tail -30 /opt/pdf-scheduler/data/logs/scheduler.log",
    "cat /opt/pdf-scheduler/data/logs/scheduler.log"
) -join "; "
proc /c "echo $plain | ssh -o StrictHostKeyChecking=no root@192.192.98.86 `"$commands`""
