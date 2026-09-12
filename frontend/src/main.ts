import { mount } from 'svelte'
import App from './App.svelte'
import './index.css'

const target = document.getElementById('root')
if (!target) throw new Error('#root is missing from index.html')

export default mount(App, { target })
