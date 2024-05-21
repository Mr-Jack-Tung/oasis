import asyncio

from social_agent.agents_generator import generate_agents
from twitter.channel import Twitter_Channel
from twitter.twitter import Twitter


async def running():
    test_db_filepath = "./data/mock_twitter.db"

    channel = Twitter_Channel()
    infra = Twitter(test_db_filepath, channel)
    task = asyncio.create_task(infra.running())

    agent_graph = await generate_agents("./data/user_all_id.csv", channel)
    for node_id, node_data in agent_graph.get_agents():
        agent = node_data['agent']
        await agent.perform_action_by_llm()

    await channel.write_to_receive_queue((None, None, "exit"))
    await task


if __name__ == "__main__":
    asyncio.run(running())
