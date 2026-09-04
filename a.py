import random
import sys
import tkinter as tk

W, H, CELL, SPEED = 30, 20, 20, 120  # 网格宽/高、每格像素、每步毫秒

root = tk.Tk()
root.title("贪吃蛇")
canvas = tk.Canvas(root, width=W * CELL, height=H * CELL, bg="black")
canvas.pack()


def reset():
    global snake, direction, score, game_over, turned
    snake = [(W // 2, H // 2)]
    direction = (1, 0)
    score = 0
    game_over = False
    turned = False
    place_food()


def place_food():
    global food
    while True:
        food = (random.randrange(W), random.randrange(H))
        if food not in snake:
            return


def draw():
    canvas.delete("all")
    for x, y in snake:
        canvas.create_rectangle(x * CELL, y * CELL, x * CELL + CELL - 1,
                                y * CELL + CELL - 1, fill="lime")
    canvas.create_rectangle(food[0] * CELL, food[1] * CELL,
                            food[0] * CELL + CELL - 1, food[1] * CELL + CELL - 1, fill="red")
    canvas.create_text(W * CELL // 2, 12, text=f"分数: {score}", fill="white",
                       font=("微软雅黑", 12))
    if game_over:
        canvas.create_text(W * CELL // 2, H * CELL // 2, text="游戏结束，按空格重开",
                           fill="white", font=("微软雅黑", 20))


def step():
    global snake, score, game_over, turned
    turned = False
    hx, hy = snake[0]
    nx, ny = hx + direction[0], hy + direction[1]
    if not (0 <= nx < W and 0 <= ny < H) or (nx, ny) in snake:  # 撞墙或撞自己
        game_over = True
        draw()
        return
    snake.insert(0, (nx, ny))
    if (nx, ny) == food:
        score += 1
        place_food()
    else:
        snake.pop()
    draw()
    root.after(SPEED, step)


TURNS = {"Up": (0, -1), "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0)}


def on_key(e):
    global direction, turned
    if game_over:
        if e.keysym == "space":
            reset()
            draw()
            root.after(SPEED, step)
        return
    if e.keysym in TURNS and not turned:  # 每步只允许转一次向，防止快速按键掉头
        d = TURNS[e.keysym]
        if (d[0] + direction[0], d[1] + direction[1]) != (0, 0):
            direction = d
            turned = True


root.bind("<Key>", on_key)

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 无窗口自检：贪心 AI 追食物跑 400 步，验证不崩且能吃到
        root.withdraw()
        reset()
        for _ in range(400):
            if game_over:
                break
            cands = [d for d in ((0, -1), (0, 1), (-1, 0), (1, 0))
                     if (d[0] + direction[0], d[1] + direction[1]) != (0, 0)
                     and 0 <= snake[0][0] + d[0] < W and 0 <= snake[0][1] + d[1] < H
                     and (snake[0][0] + d[0], snake[0][1] + d[1]) not in snake]
            if not cands:
                break
            direction = min(cands, key=lambda d:
                            abs(snake[0][0] + d[0] - food[0]) + abs(snake[0][1] + d[1] - food[1]))
            step()
        assert score > 0, f"selftest 失败: 400 步没吃到食物 (score={score})"
        print(f"selftest OK: 吃到 {score} 个食物, 蛇长 {len(snake)}, 撞死={game_over}")
        sys.exit()
    reset()
    draw()
    root.after(SPEED, step)
    root.mainloop()
