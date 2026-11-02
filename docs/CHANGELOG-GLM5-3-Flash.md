# CHANGELOG — zca GLM-5.3-Flash 反代支持（2026-08-28，第2/3阶段）
> 作用域：为 zca 网关补 GLM-5.3-Flash 反代能力。本文件为只读备查说明，不含任何真实密钥/token。

## 背景
容器 zca 之前跑预构建镜像 jiamian0128/zca:sha-95eb0c6，无 GLM-5.3-Flash 映射。
第3阶段 A 方案：本地构建规避 Docker Hub push，重建容器使代码生效。

## 4 处代码改动（host /opt/zca，均已带入本地镜像 zca-local-glm53:latest）
1. app/routes/gateway.py
   - MODEL_NAME_MAP 增键 "glm-5.3-flash": "GLM-5.3-Flash"
   - AVAILABLE_MODELS 增 "GLM-5.3-Flash"（在 GLM-5.3 之后）
2. app/start_plan.py
   - MODEL_NAME_MAP 增键 "glm-5.3-flash": "GLM-5.3-Flash"
3. app/routes/admin_api.py
   - _TEST_MODELS 增 "GLM-5.3-Flash"（后台「测试」可选）
4. app/statics/admin/accounts.html
   - 「测试模型」下拉增 <option value="GLM-5.3-Flash">
兼容性：保留 GLM-5.3/GLM-5.2/GLM-5-Turbo 映射与 AVAILABLE_MODELS，无回归（静态自检全过）。
运行期注意：MODEL_NAME_MAP 未命中输入走 passthrough；_normalize_body 对 glm-5.3-flash / GLM-5.3-Flash 均映射到 GLM-5.3-Flash。

## 部署生效（第3阶段步骤1）
- 本地构建：sudo docker build -t zca-local-glm53:latest .
- 镜像：zca-local-glm53:latest（ID 0ec3f7715fec，1.28GB）
- 重建：.env ZCA_IMAGE=zca-local-glm53:latest → docker compose -f compose.yaml up -d
- 生效验证：GET /v1/models 含 GLM-5.3-Flash；容器 healthy
- 回滚：旧镜像 jiamian0128/zca:sha-95eb0c6（本地保留，ID 62caeeb057aa）；.env 备份 /tmp/zca.env.bak.202608281105
