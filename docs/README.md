# AI 外呼系统文档目录

## 文档概览

以下是 AI 外呼系统相关的技术文档：

### 核心文档

1. **[语音路由解决方案](./voice_routing_solution.md)**
   - FreeSWITCH 语音路由架构
   - Zoiper 软电话集成方案
   - 后端 AI 服务集成
   - 完整的呼叫流程说明

2. **[实施指南](./implementation_guide.md)**
   - 详细部署步骤
   - 配置文件说明
   - 测试和故障排除
   - 性能优化建议

3. **[配置示例](./example_config.xml)**
   - FreeSWITCH 配置文件示例
   - 拨号计划配置
   - Sofia Profile 设置

## 快速开始

对于初次使用者，建议按以下顺序阅读文档：

1. 首先阅读 [语音路由解决方案](./voice_routing_solution.md) 了解整体架构
2. 然后参考 [实施指南](./implementation_guide.md) 进行部署
3. 使用 [配置示例](./example_config.xml) 作为配置基础

## 相关目录

- `freeswitch/conf/` - FreeSWITCH 主配置文件
- `backend/` - 后端服务代码
- `docker/` - Docker 部署配置