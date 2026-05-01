sleep 2
python3 -c "
import time
time.sleep(1)
print(open('/tmp/captured_exact.sh').read(), end='')
" | bash
echo "WRAPPER_DONE"
