"""Published model prompt conventions with shared DDL/evidence inputs.
Sources: seeklhy/OmniSQL-7B and XGenerationLab/XiYanSQL-QwenCoder-7B-2504
model cards; phanipal/slonik-7b/scripts/eval_bird_sqlite.py.
"""
import re
MODELS = {
    'slonik': 'hf.co/Phani-labs/Slonik-7B-GRPO-GGUF:Q4_K_M',
    'omnisql': 'hf.co/mradermacher/OmniSQL-7B-GGUF:Q4_K_M',
    'xiyan': 'hf.co/mradermacher/XiYanSQL-QwenCoder-7B-2504-GGUF:Q4_K_M',
}

def prompt_for(profile, schema, question, evidence):
    if profile == 'slonik':
        return (f'You are a SQLite expert. Given the schema below, write a single SQL query.\n\n### Schema:\n{schema}\n\n### Question:\n{question}\n'
                + (f'\n### Hint:\n{evidence}\n' if evidence else '')
                + '\nReturn ONLY the SQL query, no explanation.')
    if profile == 'xiyan':
        return (f'你是一名SQLite专家，现在需要阅读并理解下面的〖数据库schema〗描述，以及可能用到的〖参考信息〗，并运用SQLite知识生成sql语句回答〖用户问题〗。\n'
                f'〖用户问题〗\n{question}\n\n〖数据库schema〗\n{schema}\n\n〖参考信息〗\n{evidence}\n\n〖用户问题〗\n{question}\n\n```sql')
    if profile == 'omnisql':
        return f'''Task Overview:
You are a data science expert. Below, you are provided with a database schema and a natural language question. Your task is to understand the schema and generate a valid SQL query to answer the question.

Database Engine:
SQLite

Database Schema:
{schema}
This schema describes the database's structure, including tables, columns, primary keys, foreign keys, and any relevant relationships or constraints.

Question:
{question}
Provided evidence: {evidence}

Instructions:
- Make sure you only output the information that is asked in the question. If the question asks for a specific column, make sure to only include that column in the SELECT clause, nothing more.
- The generated query should return all of the information asked in the question without any missing or extra information.
- Before generating the final SQL query, please think through the steps of how to write the query.

Output Format:
In your answer, please enclose the generated SQL query in a code block:
```
-- Your SQL query
```

Take a deep breath and think step by step to find the correct SQL query.'''
    raise ValueError(profile)


def final_sql(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S)
    blocks = re.findall(r'```(?:sql|sqlite)?\s*(.*?)```', text, flags=re.S|re.I)
    candidates = [b.strip() for b in blocks if re.search(r'\b(?:SELECT|WITH)\b', b, re.I)]
    if candidates:
        return candidates[-1]
    # XiYan can complete the opening fence contained in the prompt.
    text = text.strip().removesuffix('```').strip()
    if re.match(r'^(?:SELECT|WITH)\b', text, re.I):
        return text
    raise ValueError('No unambiguous final SQL block or plain SQL response')
