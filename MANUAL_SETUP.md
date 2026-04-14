# First-time manual platform configuration
1. Login with **admin** account and wait for private project to be created (~5-10 minutes, refresh the page periodically untill **Public and Private** projects are present in dropdown)


2. [PGVector] In **Public** project **Settings -> AI Configuration** create new **PgVector** credential:
    - Label: **elitea-pgvector**
    - Shared: **NO** (do not check)
    - Set as default: **NO** (do not check)
    - Connection string: **postgresql+psycopg://POSTGRES\_USER:POSTGRES\_PASSWORD@postgres:POSTGRES\_PORT/POSTGRES\_DB** use values from your default.env/override.env; example: **postgresql+psycopg://centry:changeme@postgres:5432/db**


3. [PGVector] In **Administration** mode **Tasks** **/admin/app/schedules-tasks** start:
    - Task: **sync\_pgvector\_credentials**
    - Parameter (input below task dropdown): **force\_recreate,save\_connstr\_to\_secrets**


4. [LiteLLM] In **Administration** mode **Tasks** **/admin/app/schedules-tasks** start:
    - Task: **create\_database**
    - Parameter (input below task dropdown): **litellm**


5. [LiteLLM] In **Tasks** table open logs for created task (usually last one with name: **create\_database**) and copy **connection string** from log.


6. [LiteLLM] In **Administration** mode **Advanced** **/admin/app/configuration** open **runtime\_engine\_litellm** config
    - Click **View**
    - Change **database\_url** from **null** to copied **connection string**
    - Generate master key in format **sk-[alphanumeric]** (example: **sk-abcd1234ABCD**)
    - Change **litellm\_master\_key** to generated master key
    - Click **Save**
    > Example:
    > ```
    > database_url: postgresql://aaa:bbb@ccc:5432/litellm
    > litellm_master_key: sk-abcd1234ABCD
    > ```


7. [LiteLLM] In **Administration** mode **Advanced** **/admin/app/configuration** find **pylon-indexer**
    - Click **Restart**
    - Wait for **pylon-indexer** to restart


8. [LiteLLM] In **Administration** mode **Tasks** **/admin/app/schedules-tasks** start:
    - Task: **sync\_llm\_entities**
    - Parameter: **(leave empty)**


9. [Agent state] In **Administration** mode **Tasks** **/admin/app/schedules-tasks** start:
    - Task: **create\_database**
    - Parameter (input below task dropdown): **agentstate**


10. [Agent state] In **Tasks** table open logs for created task (usually last one with name: **create\_database**) and copy **connection string** from log.


11. [Agent state] In **Administration** mode **Advanced** **/admin/app/configuration** open **indexer\_worker** config
    - Click **View**
    - Uncomment **agent_memory_config** block
    - Change **connection\_string** to copied **connection string**
    - Click **Save**
    > Example:
    > ```
    > agent_memory_config:
    >   type: postgres
    >   connection_string: postgresql://aaa:bbb@ccc:5432/agentstate
    >   connection_kwargs:
    >     autocommit: true
    > ```
