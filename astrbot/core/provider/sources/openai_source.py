import base64
import json
import os

from openai import AsyncOpenAI, AsyncAzureOpenAI, NOT_GIVEN
from openai.types.chat.chat_completion import ChatCompletion
from openai._exceptions import NotFoundError
from astrbot.core.utils.io import download_image_by_url

from astrbot.core.db import BaseDatabase
from astrbot.api.provider import Provider, Personality
from astrbot import logger
from astrbot.core.provider.func_tool_manager import FuncCall
from typing import List
from ..register import register_provider_adapter
from astrbot.core.provider.entites import LLMResponse

@register_provider_adapter("openai_chat_completion", "OpenAI API Chat Completion 提供商适配器")
class ProviderOpenAIOfficial(Provider):
    def __init__(
        self, 
        provider_config: dict, 
        provider_settings: dict,
        db_helper: BaseDatabase, 
        persistant_history = True,
        default_persona: Personality = None
    ) -> None:
        super().__init__(provider_config, provider_settings, persistant_history, db_helper, default_persona)
        self.chosen_api_key = None
        self.api_keys: List = provider_config.get("key", [])
        self.chosen_api_key = self.api_keys[0] if len(self.api_keys) > 0 else None
        
        # 适配 azure openai #332
        if "api_version" in provider_config:
            # 使用 azure api
            self.client = AsyncAzureOpenAI(
                api_key=self.chosen_api_key,
                api_version=provider_config.get("api_version", None),
                base_url=provider_config.get("api_base", None),
                timeout=provider_config.get("timeout", NOT_GIVEN),
            )
        else:
            # 使用 openai api
            self.client = AsyncOpenAI(
                api_key=self.chosen_api_key,
                base_url=provider_config.get("api_base", None),
                timeout=provider_config.get("timeout", NOT_GIVEN),
            )
            
        self.set_model(provider_config['model_config']['model'])
    
    async def get_human_readable_context(self, session_id, page, page_size):
        if session_id not in self.session_memory:
            raise Exception("会话 ID 不存在")
        contexts = []
        temp_contexts = []
        for record in self.session_memory[session_id]:
            if record['role'] == "user":
                temp_contexts.append(f"User: {record['content']}")
            elif record['role'] == "assistant":
                temp_contexts.append(f"Assistant: {record['content']}")
                contexts.insert(0, temp_contexts)
                temp_contexts = []

        # 展平 contexts 列表
        contexts = [item for sublist in contexts for item in sublist]

        # 计算分页
        paged_contexts = contexts[(page-1)*page_size:page*page_size]
        total_pages = len(contexts) // page_size
        if len(contexts) % page_size != 0:
            total_pages += 1
        
        return paged_contexts, total_pages

    async def get_models(self):
        try:
            models_str = []
            models = await self.client.models.list()
            models = models.data
            for model in models:
                models_str.append(model.id)
            return models_str
        except NotFoundError as e:
            raise Exception(f"获取模型列表失败：{e}")
    
    async def pop_record(self, session_id: str):
        '''
        弹出最早的一个对话
        '''
        if session_id not in self.session_memory:
            raise Exception("会话 ID 不存在")

        if len(self.session_memory[session_id]) < 2:
            return
        
        try:
            self.session_memory[session_id].pop(0)
            self.session_memory[session_id].pop(0)
        except IndexError:
            pass
    
    async def _query(self, payloads: dict, tools: FuncCall) -> LLMResponse:
        if tools:
            tool_list = tools.get_func_desc_openai_style()
            if tool_list:
                payloads['tools'] = tool_list
        
        completion = await self.client.chat.completions.create(
            **payloads,
            stream=False
        )

        assert isinstance(completion, ChatCompletion)
        logger.debug(f"completion: {completion}")

        if len(completion.choices) == 0:
            raise Exception("API 返回的 completion 为空。")
        choice = completion.choices[0]
        
        if choice.message.content:
            # text completion
            completion_text = str(choice.message.content).strip()

            return LLMResponse("assistant", completion_text, raw_completion=completion)
        elif choice.message.tool_calls:
            # tools call (function calling)
            args_ls = []
            func_name_ls = []
            for tool_call in choice.message.tool_calls:
                for tool in tools.func_list:
                    if tool.name == tool_call.function.name:
                        args = json.loads(tool_call.function.arguments)
                        args_ls.append(args)
                        func_name_ls.append(tool_call.function.name)
            return LLMResponse(role="tool", tools_call_args=args_ls, tools_call_name=func_name_ls, raw_completion=completion)
        else:
            logger.error(f"API 返回的 completion 无法解析：{completion}。")
            raise Exception("Internal Error")

    async def text_chat(
        self,
        prompt: str,
        session_id: str,
        image_urls: List[str]=None,
        func_tool: FuncCall=None,
        contexts=None,
        system_prompt=None,
        **kwargs
    ) -> LLMResponse: 
        new_record = await self.assemble_context(prompt, image_urls)
        context_query = []
        if not contexts:
            context_query = [*self.session_memory[session_id], new_record]
        else:
            context_query = [*contexts, new_record]
        if system_prompt:
            context_query.insert(0, {"role": "system", "content": system_prompt})

        for part in context_query:
            if '_no_save' in part:
                del part['_no_save']

        payloads = {
            "messages": context_query,
            **self.provider_config.get("model_config", {})
        }
        llm_response = None
        try:
            llm_response = await self._query(payloads, func_tool)
        except Exception as e:
            if "maximum context length" in str(e):
                # 重试 10 次
                retry_cnt = 10
                while retry_cnt > 0:
                    logger.warning("上下文长度超过限制。尝试弹出最早的记录然后重试。")
                    try:
                        await self.pop_record(session_id)
                        llm_response = await self._query(payloads, func_tool)
                        break
                    except Exception as e:
                        if "maximum context length" in str(e):
                            retry_cnt -= 1
                        else:
                            raise e
                if retry_cnt == 0:
                    llm_response = LLMResponse("err", "err: 请尝试 /reset 清除会话记录。")
            elif "The model is not a VLM" in str(e): # siliconcloud
                # 尝试删除所有 image
                new_contexts = await self._remove_image_from_context(context_query)
                payloads['messages'] = new_contexts
                llm_response = await self._query(payloads, func_tool)

            # openai, ollama, gemini openai, siliconcloud 的错误提示与 code 不统一，只能通过字符串匹配
            elif 'does not support Function Calling' in str(e) \
                or 'does not support tools' in str(e)  \
                or 'Function call is not supported' in str(e) \
                or 'Function calling is not enabled' in str(e) \
                or 'Tool calling is not supported' in str(e): # siliconcloud 
                    logger.info(f"{self.get_model()} 不支持函数调用工具调用，已经自动去除")
                    if 'tools' in payloads:
                        del payloads['tools']
                    llm_response = await self._query(payloads, None)
            else:
                logger.error(f"发生了错误。Provider 配置如下: {self.provider_config}")
                
                if 'tool' in str(e).lower() and 'support' in str(e).lower():
                    logger.error(f"疑似该模型不支持函数调用工具调用。请输入 /tool off_all")
                
                if 'Connection error.' in str(e):
                    proxy = os.environ.get("http_proxy", None)
                    if proxy:
                        logger.error(f"可能为代理原因，请检查代理是否正常。当前代理: {proxy}")
                
                raise e
        
        if kwargs.get("persist", True) and llm_response:
            await self.save_history(contexts, new_record, session_id, llm_response)
        
        return llm_response
    
    async def _remove_image_from_context(self, contexts: List):
        '''
        从上下文中删除所有带有 image 的记录
        '''
        new_contexts = []
        
        flag = False
        for context in contexts:
            if flag:
                flag = False # 删除 image 后，下一条（LLM 响应）也要删除
                continue
            if isinstance(context['content'], list):
                flag = True
                # continue
                new_content = []
                for item in context['content']:
                    if isinstance(item, dict) and 'image_url' in item:
                        continue
                    new_content.append(item)
                if not new_content:
                    # 用户只发了图片
                    new_content = [{"type": "text", "text": "[图片]"}]
                context['content'] = new_content
            new_contexts.append(context)
        return new_contexts

    
    async def save_history(self, contexts: List, new_record: dict, session_id: str, llm_response: LLMResponse):
        if llm_response.role == "assistant" and session_id:
            # 文本回复
            if not contexts:
                # 添加用户 record
                self.session_memory[session_id].append(new_record)
                # 添加 assistant record
                self.session_memory[session_id].append({
                    "role": "assistant",
                    "content": llm_response.completion_text
                })
            else:
                contexts_to_save = list(filter(lambda item: '_no_save' not in item, contexts))
                self.session_memory[session_id] = [*contexts_to_save, new_record, {
                    "role": "assistant",
                    "content": llm_response.completion_text
                }]
            self.db_helper.update_llm_history(session_id, json.dumps(self.session_memory[session_id]), self.provider_config['id'])
        
    async def forget(self, session_id: str) -> bool:
        self.session_memory[session_id] = []
        self.db_helper.update_llm_history(session_id, json.dumps(self.session_memory[session_id]), self.provider_config['id'])
        return True

    def get_current_key(self) -> str:
        return self.client.api_key

    def get_keys(self) -> List[str]:
        return self.api_keys
    
    def set_key(self, key):
        self.client.api_key = key
        
    async def assemble_context(self, text: str, image_urls: List[str] = None):
        '''
        组装上下文。
        '''
        if image_urls:
            user_content = {"role": "user","content": [{"type": "text", "text": text}]}
            for image_url in image_urls:
                if image_url.startswith("http"):
                    image_path = await download_image_by_url(image_url)
                    image_data = await self.encode_image_bs64(image_path)
                else:
                    if image_url.startswith("file:///"):
                        image_url = image_url.replace("file:///", "")
                    image_data = await self.encode_image_bs64(image_url)
                user_content["content"].append({"type": "image_url", "image_url": {"url": image_data}})
            return user_content
        else:
            return {"role": "user","content": text}

    async def encode_image_bs64(self, image_url: str) -> str:
        '''
        将图片转换为 base64
        '''
        if image_url.startswith("base64://"):
            return image_url.replace("base64://", "data:image/jpeg;base64,")
        with open(image_url, "rb") as f:
            image_bs64 = base64.b64encode(f.read()).decode('utf-8')
            return "data:image/jpeg;base64," + image_bs64
        return ''