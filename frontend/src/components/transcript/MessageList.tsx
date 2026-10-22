import type { SessionMessage } from "../../api/sessions";

import { MessageItem } from "./MessageItem";

interface Props {
  messages: readonly SessionMessage[];
}

export function MessageList({ messages }: Props) {
  return (
    <div className="flex flex-col">
      {messages.map((m) => (
        <MessageItem key={m.turn_index} message={m} />
      ))}
    </div>
  );
}
