import re
from pathlib import Path

import yaml


def load_documents(directory):
    documents = []
    for path in Path(directory).glob("*.md"):
        try:
            _, header, content = path.read_text().split("---", 2)
            metadata = yaml.safe_load(header)
            required = ("id", "title", "team")
            if not isinstance(metadata, dict) or not all(isinstance(metadata.get(key), str) for key in required):
                continue
            roles = metadata.get("allowed_roles")
            if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles):
                continue
            if metadata.get("environment") != "demo":
                continue
            documents.append({**metadata, "content": content.strip()})
        except (ValueError, yaml.YAMLError, OSError):
            continue
    return documents


def select_document(documents, actor, message, status):
    allowed = {doc["id"]: doc for doc in documents if doc["team"] == actor["team"] and actor["role"] in doc["allowed_roles"]}
    if not re.search(r"доступ|403|заявк|согласован|фикс|исправлен|провер|стенд|статус|catalog", message, re.I):
        return None
    document_id = "catalog-dev-access"
    if status == "granted":
        document_id = "catalog-dev-verify"
    if status in ("granted", "unknown"):
        if re.search(r"403|не пуска|нет доступа|не могу (?:попасть|войти)", message, re.I):
            document_id = "catalog-dev-403"
    return allowed.get(document_id)


def another_resource(message):
    return bool(re.search(r"друг[а-я]*\s+стенд|\b(?:prod|production|staging|payments|billing)\b|платеж|платёж|прод[еа]?\b|два стенда|несколько стендов", message, re.I))
