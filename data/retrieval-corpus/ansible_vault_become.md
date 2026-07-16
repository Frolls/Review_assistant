# Ansible Vault и privilege escalation

Источники:

- https://docs.ansible.com/projects/ansible/latest/vault_guide/index.html
- https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_privilege_escalation.html

Ansible Vault шифрует variables или файлы, чтобы чувствительные значения можно было хранить рядом с automation-кодом в зашифрованном виде. Vault защищает data at rest, но после расшифровки значение существует в памяти процесса и может попасть в output, template или удалённый файл.

Vault password не хранится в том же repository, что и encrypted content. Он передаётся интерактивно, через защищённый password file, vault identity или интеграцию с secret manager. CI credential имеет минимальный scope и управляется отдельно от исходного кода.

Шифрование отдельной variable удобно для небольших значений, а encrypted file — для набора секретов. Имена variables можно оставлять открытыми для понимания структуры, если сами имена не раскрывают чувствительную информацию. Выбор должен поддерживать удобную ротацию и review изменения.

Vault ID позволяет использовать несколько паролей или областей, например dev и production. Команда расшифрования должна однозначно выбирать identity. Один общий пароль для всех окружений увеличивает blast radius компрометации.

`no_log: true` предотвращает обычный вывод аргументов и результата task, содержащих secret, но снижает наблюдаемость и не устраняет все внешние каналы. Его применяют точечно. Debug task, failed template и зарегистрированные variables проверяются на отсутствие утечки.

Template, создающий secret file, должен задавать ограниченный owner, group и mode. Diff для такой task отключается, иначе `--diff` может записать секрет в CI log. Временные файлы, backup и remote cache также учитываются в модели угроз.

`become` включает privilege escalation, а `become_user` задаёт целевого пользователя; одно не включает другое автоматически. Метод escalation зависит от платформы и connection plugin. Credentials или password для become передаются защищённо, а не в playbook.

Повышенные права следует ограничивать конкретными tasks или узким block. `become: true` на уровне всего play удобен, но любая добавленная позже task автоматически выполняется привилегированно. Принцип least privilege требует минимального времени и набора операций под escalation.

Remote user и become user могут быть непривилегированными. В этом случае Ansible должен безопасно передать временный module file между пользователями; особенности ACL, shared group и world-readable temporary files влияют на риск. Не следует включать небезопасный fallback без понимания платформы.

Если playbook зависает на escalation, возможна интерактивная password prompt. Automation pipeline не должен ожидать terminal input: credential configuration и метод become проверяются заранее. Ошибка доступа не маскируется `ignore_errors`, поскольку последующие tasks могут создать частично настроенную систему.

На network devices become может означать переход в privileged EXEC mode и требует подходящего connection type. Настройки для Linux sudo нельзя механически переносить на network automation или Windows. Role документирует поддерживаемые platforms и методы.

Ротация секрета включает обновление источника, повторное шифрование при необходимости, безопасное применение и отзыв старого значения. PR с encrypted blob всё равно требует объяснения назначения изменения и способа проверки, хотя reviewer не видит plaintext в diff.

Vault не заменяет внешний secret manager, когда нужны динамические credentials, аудит выдачи, короткий TTL или централизованная ротация. В таком случае Ansible получает секрет во время запуска и не сохраняет его в repository. Архитектурный выбор зависит от жизненного цикла и модели доступа.

На ревью совместно проверяются хранение, доставка и использование секрета: где лежит ключ расшифрования, кто имеет доступ, не выводится ли значение, какие permissions получает remote file и действительно ли privileged task требует escalation.
