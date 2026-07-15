# Observability Review

Ревью observability проверяет, сможет ли команда расследовать incident после merge. Важные операции должны писать structured logs с request id, стабильным event name, latency, outcome и безопасным набором параметров.

Логи не должны содержать секреты, полный пользовательский prompt, приватный diff, cookie или персональные данные. Если нужен preview, он должен проходить redaction до записи. Маскирование после логирования не считается защитой.

Для внешних вызовов нужны метрики latency, error rate и retry count. Если новый HTTP-клиент или LLM-клиент не даёт понять, какой backend упал и сколько занял запрос, ревьюер должен попросить добавить instrumentation.

Trace полезен только при контроле содержимого. В production-like окружениях raw input/output обычно выключены, а диагностика строится по request id, hashes, source names, scores и кодам ошибок.

Alert должен быть привязан к пользовательскому влиянию: рост 5xx, timeouts, rate limit, empty responses, падение cache hit rate или деградация retrieval score. Метрика без понятного действия создаёт шум.
