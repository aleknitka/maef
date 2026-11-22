import { createCliRenderer } from "@opentui/core"
import { createRoot } from "@opentui/react"
import { App } from "./App.tsx"
import { createApi, DEFAULT_URL } from "./api.ts"

const url = process.env.MAEF_API_URL || DEFAULT_URL
if (!/^https?:\/\/[^/]+/.test(url)) {
  console.error("MAEF_API_URL must be an http(s) URL.")
  process.exit(2)
}

const renderer = await createCliRenderer({ exitOnCtrlC: false })
const quit = () => { renderer.destroy(); process.exit(0) }
createRoot(renderer).render(<App api={createApi(url)} onQuit={quit} />)
